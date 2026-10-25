#!/usr/bin/env python3
"""midi-brain -- Hooligan Soup MIDI router.

Cross-platform: runs unmodified on ringo (Linux/Raspberry Pi, primary) and
Constellation (Windows, backup). Platform differences are isolated behind
IS_WINDOWS -- everything else (song table, lighting logic, OSC handling) is
shared code.

Source of truth: song_profile_assignments - CURRENT (Google Drive spreadsheet)
Lighting control via QLC+ native functions (WebSocket setFunctionStatus API).
QLC+ workspace must be running on both ringo and Constellation.

Lighting mood → workspace note ranges (from VC button MIDI input assignments):
  High Energy:  chases 0-3   (no separate scenes)
  Atmospheric:  scenes 4-13  chases 14-23
  Purple Rain:  scenes 24-33 chases 34-43
  Default:      scenes 44-53 chases 54-63
  Floyd:        scenes 69-78 chases 79-88
  Special:      All Scene Chaser=64  All Chasers=65  Strobe=66
                Base Scene=67  Blackout=68

VE-500 patch PC values (0-indexed) -- per-key scheme, 20 physical patches.
Patch names are BASE/BIG/LNGDLY + chart key; BIG merges the old WET/3PT UP/
3PT WRAP patches into one. SHOUT/RADIO/LOWOCT are key-independent.
  U01 BASE-A=0    U02 BASE-B=1    U03 BASE-Bb=2   U04 BASE-C=3
  U05 BASE-D=4    U06 BASE-E=5    U07 BASE-F=6    U08 BASE-F#=7
  U09 BASE-G=8    U10 BIG-A=9     U11 BIG-C=10    U12 BIG-D=11
  U13 BIG-E=12    U14 BIG-G=13    U15 BIG-Em=14   U16 LNGDLY-A=15
  U17 LNGDLY-D=16 U18 SHOUT=17    U19 RADIO=18    U20 LOWOCT=19
Source of truth for the name->key mapping: song_profile_assignments -
CURRENT sheet's Vox Preset column. Patches are pre-tuned a semitone down
on the VE-500 itself (band tunes down a semitone), so chart key == patch
name key; no software transposition needed.

HX Stomp: Regular=116  Clean=117  Metal=118

Windows (Constellation) one-time setup, used only when ringo is down:
  1. pip install python-rtmidi python-osc websocket-client
  2. loopMIDI   https://www.tobias-erichsen.de/software/loopmidi.html
       Uses existing port: QLC_Input
  3. rtpMIDI    https://www.tobias-erichsen.de/software/rtpmidi.html
       Session named exactly: Constellation (runs always-on, no collision
       with ringo's rtpmidid session, which announces itself as "ringo")
       On the BandHelper app / whatever sends MIDI: point its network MIDI
       destination at "ringo" normally, "Constellation" during failover.
  4. Unplug USB hub from ringo, plug into Constellation
  5. Launch QLC+ with "Hooligan MASTER.qxw" (or use the desktop failover shortcut)
  6. python midi-brain.py

Linux (ringo) setup is handled by the ringo-midi-brain systemd service --
see /etc/systemd/system/ringo-midi-brain.service.
"""

import rtmidi
import time
import logging
import sys
import platform
import threading
import queue as _queue
import re

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger('brain')

IS_WINDOWS = sys.platform.startswith('win')

# ---------------------------------------------------------------------------
# Port name fragments
# ---------------------------------------------------------------------------
PORT_MOX6       = 'U2MIDI'
PORT_HXSTOMP    = 'HX Stomp'
PORT_VE500      = 'VE-500'

if IS_WINDOWS:
    PORT_BANDHELPER = 'Constellation'  # rtpMIDI session name (Tobias Erichsen)
    PORT_QLC_BRIDGE = 'QLC_Input'    # loopMIDI port name
    BANDHELPER_RETRY_S = 20
else:
    PORT_BANDHELPER = 'Network Export'  # rtpmidid -- BandHelper sends here
    QLC_RAWMIDI = '/dev/snd/midiC2D0'
    BANDHELPER_RETRY_S = 15

OSC_PORT = 9000  # BandHelper OSC -> brain
PORT_SCAN_INTERVAL_S = 0.5  # detect USB/service re-enumeration quickly

# ---------------------------------------------------------------------------
# Lighting moods: (scene_notes, chase_notes)
# Notes match VC button MIDI input assignments in Hooligan MASTER.qxw.
# scene_notes=None → mood has no static scenes; PREV/NEXT SCENE uses chases.
# ---------------------------------------------------------------------------
LIGHTING = {
    'High Energy':  (None,               [0,  1,  2,  3]),
    'Atmospheric':  (list(range( 4, 14)), list(range(14, 24))),
    'Purple Rain':  (list(range(24, 34)), list(range(34, 44))),
    'Default':      (list(range(44, 54)), list(range(54, 64))),
    'Floyd':        (list(range(69, 79)), list(range(79, 89))),
}

# ---------------------------------------------------------------------------
# Song lookup table -- built from song_profile_assignments - CURRENT
# Keys: pp (0-indexed PC from MOX6)
# Values: (song_name, mood, rr, ve_pc)
#   rr = HX Stomp PC  (116=Regular 117=Clean 118=Metal)
#   ve_pc = VE-500 PC, per-key scheme (see docstring at top of file)
# ---------------------------------------------------------------------------
def _song(lighting, vox_pc, rr, name):
    return (name, lighting, rr, vox_pc)

R  = 116  # Regular
CL = 117  # Clean
MT = 118  # Metal

# vox PC shortcuts -- one per physical VE-500 patch (see docstring table)
BASE_A   = 0
BASE_B   = 1
BASE_BB  = 2   # Bb
BASE_C   = 3
BASE_D   = 4
BASE_E   = 5
BASE_F   = 6
BASE_FS  = 7   # F#
BASE_G   = 8
BIG_A    = 9
BIG_C    = 10
BIG_D    = 11
BIG_E    = 12
BIG_G    = 13
BIG_EM   = 14  # Em
LNGDLY_A = 15
LNGDLY_D = 16
SHOUT    = 17
RADIO    = 18
LOWOCT   = 19

SONGS = {
    1:  _song('High Energy', BASE_A,   R,  'Rebel Yell'),
    2:  _song('High Energy', BASE_A,   R,  'Rebel Yell'),
    3:  _song('Default',     BASE_D,   R,  'Short Skirt Long Jacket'),
    4:  _song('Floyd',       LNGDLY_D, R,  'Comfortably Numb'),
    5:  _song('Default',     BASE_D,   R,  'The Distance'),
    6:  _song('Default',     BASE_C,   R,  'I Will Survive'),
    7:  _song('High Energy', BASE_C,   R,  'Boys of Summer'),
    8:  _song('Floyd',       BASE_G,   R,  'Have A Cigar'),
    9:  _song('Atmospheric', BASE_A,   R,  'LA Woman'),
    10: _song('Atmospheric', BIG_G,    R,  'Lost Together'),
    11: _song('Atmospheric', BIG_C,    R,  'Lovesong'),
    12: _song('High Energy', BASE_B,   R,  'Pump It Up'),
    13: _song('Default',     BASE_D,   R,  'White Wedding'),
    14: _song('Default',     BASE_BB,  R,  'Bad Guy'),
    # pp=15 (A16): split off pp9, which used to be shared with LA Woman --
    # its chart key (E) needs a different VE-500 patch than LA Woman's (A).
    # MOX6 A16 duplicated from A10, BandHelper PP updated to 15 -- live as
    # of 2026-08-19.
    15: _song('Atmospheric', BASE_E,   R,  'Love Me Two Times'),
    16: _song('Floyd',       LNGDLY_A, R,  'Time'),
    17: _song('Floyd',       LNGDLY_A, R,  'Time'),
    18: _song('Floyd',       LNGDLY_A, R,  'Time'),
    32: _song('Default',     BIG_D,    CL, 'American Girl'),
    33: _song('Default',     BASE_D,   R,  'Are You Gonna Be My Girl'),
    34: _song('Default',     BIG_D,    R,  'Banditos'),
    35: _song('Default',     BIG_C,    R,  'Brown Sugar'),
    36: _song('Default',     BASE_A,   R,  'Courage'),
    37: _song('Default',     BASE_E,   R,  'Fifty Mission Cap'),
    38: _song('Default',     BASE_B,   R,  'Flagpole Sitta'),
    39: _song('Default',     BASE_D,   R,  'Fortunate Son'),
    40: _song('Default',     BIG_A,    CL, 'Here Comes My Girl'),
    41: _song('Default',     BASE_A,   R,  'Hey Jealousy'),
    42: _song('Default',     LOWOCT,   R,  'La Grange'),
    43: _song('Default',     BASE_E,   R,  'Little Bones'),
    44: _song('Default',     BASE_E,   CL, "Runnin' Down A Dream"),
    45: _song('Default',     BASE_C,   CL, 'Santeria'),
    46: _song('Default',     BASE_G,   R,  'Take Me Out'),
    47: _song('Default',     BIG_G,    R,  'The Boys Are Back In Town'),
    48: _song('Default',     BASE_C,   R,  'The Middle'),
    49: _song('Default',     BASE_A,   R,  'Three Pistols'),
    50: _song('Default',     BIG_G,    R,  'You May Be Right'),
    51: _song('Default',     BASE_E,   R,  'You Wreck Me'),
    52: _song('High Energy', BASE_B,   R,  'All The Small Things'),
    53: _song('High Energy', BASE_E,   MT, 'Basket Case'),
    54: _song('High Energy', SHOUT,    R,  'Blitzkrieg Bop'),
    55: _song('High Energy', BASE_C,   R,  'Breaking The Law'),
    56: _song('High Energy', SHOUT,    R,  'Fight For Your Right To Party'),
    57: _song('High Energy', BASE_F,   R,  'Flavor Of The Weak'),
    58: _song('High Energy', SHOUT,    R,  'I Wanna Be Sedated'),
    59: _song('High Energy', BASE_G,   R,  'Mrs Robinson'),
    60: _song('High Energy', SHOUT,    MT, 'One Eyed Bastard'),
    61: _song('High Energy', BASE_B,   R,  'Ring Of Fire'),
    62: _song('High Energy', BASE_E,   MT, 'Seek And Destroy'),
    63: _song('High Energy', SHOUT,    R,  'The Rock Show'),
    64: _song('High Energy', BASE_E,   MT, 'Welcome To Paradise'),
    65: _song('High Energy', BASE_FS,  R,  "What's My Age Again"),
    66: _song('High Energy', BIG_EM,   R,  'Young Lust'),
    67: _song('Atmospheric', BASE_B,   R,  'Red House'),
    68: _song('Atmospheric', BIG_E,    R,  'Roadhouse Blues'),
    69: _song('Purple Rain', LNGDLY_A, R,  'Purple Rain'),
    # added 2026-10-25 -- MOX6 masters E7-E11 still to be created on the keyboard
    70: _song('High Energy', BIG_A,    R,  'American Psycho'),
    71: _song('Default',     BASE_A,   R,  "Diggin' A Hole"),
    72: _song('Default',     BASE_A,   R,  'Psycho Killer'),
    73: _song('High Energy', BASE_E,   R,  'Round And Round'),
    74: _song('Default',     BASE_G,   R,  "She's So Cold"),
}

DEFAULT_SONG = _song('Default', BASE_A, R, '(unknown)')

# ---------------------------------------------------------------------------
# Lighting control notes from MOX6 (Bome Preset.0)
# ---------------------------------------------------------------------------
NOTE_PREV_SCENE  = 0x23  # 35 C2
NOTE_NEXT_SCENE  = 0x25  # 37 D2
NOTE_SCENE_CHORD = 0x24  # 36 C#2  soft=all-scene-chaser  hard=base-scene
NOTE_CHASE_CHORD = 0x26  # 38 D#2  soft=all-chasers       hard=strobe
NOTE_PREV_CHASE  = 0x27  # 39 E2
NOTE_NEXT_CHASE  = 0x28  # 40 F2
NOTE_BLACKOUT    = 0x29  # 41 F#2

VELOCITY_HARD = 80  # above this = hard press

# QLC+ function IDs for special functions (from workspace VC button mappings)
QLC_FN_ALL_SCENE_CHASER = 18   # note 64: "All Scene Chaser"
QLC_FN_ALL_CHASERS      = 0    # note 65: "All Chasers - Slow"
QLC_FN_STROBE           = 148  # note 66: "Strobe"
QLC_FN_BASE_SCENE       = 41   # note 67: "Base Scene"
QLC_FN_BLACKOUT         = 9    # note 68: "Blackout" (Scene, all channels 0)

WORKSPACE_PATH = '/home/toddz/QLC+/Hooligan MASTER.qxw'


def _parse_note_fn():
    """Parse workspace VC buttons → {midi_note: qlc_function_id}."""
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(WORKSPACE_PATH)
    except Exception as e:
        log.warning(f"workspace parse failed: {e}")
        return {}
    root = tree.getroot()
    tag = root.tag
    ns = tag.split('}')[0].lstrip('{') if '}' in tag else ''
    P = f'{{{ns}}}' if ns else ''
    engine = root.find(f'{P}Engine')
    fn_map = {int(f.get('ID')): f for f in engine.findall(f'{P}Function') if f.get('ID')}

    result = {}
    for btn in root.iter(f'{P}Button'):
        ic = btn.find(f'{P}Input')
        fe = btn.find(f'{P}Function')
        if ic is not None and fe is not None and fe.get('ID') is not None:
            ch = int(ic.get('Channel', -1))
            note = ch - 144
            fn_id = int(fe.get('ID'))
            if 0 <= note <= 127 and fn_id < 2**31:
                fn = fn_map.get(fn_id)
                name = fn.get('Name', '?') if fn is not None else '?'
                result[note] = fn_id
                log.debug(f"note {note:3d} -> fn {fn_id:4d} ({name})")
    log.info(f"workspace: {len(result)} note→function mappings loaded")
    return result


NOTE_FN = {}  # note → QLC+ function ID; loaded in main()

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
current_mood   = 'Default'
current_scenes = list(range(44, 54))  # active mood's scene note list
current_chases = list(range(54, 64))  # active mood's chase note list
ga = 0   # scene index within current_scenes
gb = 0   # chase index within current_chases

_current_fn_id = None   # QLC+ function ID currently running (None = nothing)
_last_blackout_t = 0.0  # time of last BLACKOUT press, for debounce
BLACKOUT_DEBOUNCE_S = 0.4

_note_q = _queue.Queue(maxsize=32)  # MIDI events; main loop never blocks


# ---------------------------------------------------------------------------
# Port helpers
# ---------------------------------------------------------------------------

_input_scanner = None
_output_scanner = None
_input_scanner_lock = threading.Lock()
_output_scanner_lock = threading.Lock()
_input_ports_cache = []
_output_ports_cache = []
_input_ports_cached_at = 0.0
_output_ports_cached_at = 0.0
PORT_CACHE_S = 0.25


def list_inputs():
    """List inputs through one persistent client (avoids ALSA client leaks)."""
    global _input_scanner, _input_ports_cache, _input_ports_cached_at
    with _input_scanner_lock:
        now = time.monotonic()
        if now - _input_ports_cached_at < PORT_CACHE_S:
            return list(_input_ports_cache)
        if _input_scanner is None:
            _input_scanner = rtmidi.MidiIn()
        _input_ports_cache = _input_scanner.get_ports()
        _input_ports_cached_at = now
        return list(_input_ports_cache)


def list_outputs():
    """List outputs through one persistent client (thread-safe for monitors)."""
    global _output_scanner, _output_ports_cache, _output_ports_cached_at
    with _output_scanner_lock:
        now = time.monotonic()
        if now - _output_ports_cached_at < PORT_CACHE_S:
            return list(_output_ports_cache)
        if _output_scanner is None:
            _output_scanner = rtmidi.MidiOut()
        _output_ports_cache = _output_scanner.get_ports()
        _output_ports_cached_at = now
        return list(_output_ports_cache)


def find_port(ports, fragment):
    fl = fragment.lower()
    for i, p in enumerate(ports):
        if fl in p.lower():
            return i
    return None


def alsa_target_connected(port_name, relationship):
    """Verify a target port still has its ALSA subscription.

    ``relationship`` is the line shown on the target: ``Connected From`` for
    a brain output, or ``Connecting To`` for a brain input. This supplements
    port-name monitoring when ALSA quickly reuses the same client number.
    Non-Linux platforms and unavailable procfs fail open to name monitoring.
    """
    if IS_WINDOWS:
        return True
    match = re.search(r'(\d+):(\d+)$', port_name or '')
    if not match:
        return True
    wanted_client, wanted_port = map(int, match.groups())
    try:
        with open('/proc/asound/seq/clients', encoding='utf-8') as seq_file:
            lines = seq_file.readlines()
    except OSError:
        return True

    client = None
    in_wanted_port = False
    prefix = f'{relationship}:'
    for line in lines:
        client_match = re.match(r'Client\s+(\d+)\s+:', line)
        if client_match:
            client = int(client_match.group(1))
            in_wanted_port = False
            continue
        port_match = re.match(r'\s+Port\s+(\d+)\s+:', line)
        if port_match:
            in_wanted_port = (
                client == wanted_client and
                int(port_match.group(1)) == wanted_port
            )
            continue
        if in_wanted_port and line.strip().startswith(prefix):
            return True
    return False


class DummyOut:
    """Stand-in for hardware that isn't connected. Windows' MMAPI backend
    can't open virtual ports (unlike ALSA on ringo), so just drop messages."""
    def send_message(self, data):
        pass

    def close_port(self):
        pass


class LazyMidiOut:
    """MIDI output that follows a hot-plugged/re-enumerated device.

    ALSA silently removes a subscription when a USB MIDI device disappears;
    RtMidi may still accept sends on that stale handle. Comparing the full
    advertised port name catches re-enumeration and rebuilds the subscription.
    """
    def __init__(self, fragment, label):
        self._fragment = fragment
        self._label = label
        self._out = None
        self._port_name = None
        self._last_scan = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        with self._lock:
            self._refresh_locked(force=True)
        self._monitor = threading.Thread(
            target=self._monitor_ports, daemon=True,
            name=f'midi-out-{label}')
        self._monitor.start()

    def _disconnect_locked(self, reason=None):
        old_name = self._port_name
        if self._out is not None:
            try:
                self._out.close_port()
            except Exception:
                pass
            try:
                self._out.delete()
            except Exception:
                pass
        self._out = None
        self._port_name = None
        if reason and old_name:
            log.warning(f"{self._label}: disconnected ({reason}); waiting for device")

    def _refresh_locked(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_scan < PORT_SCAN_INTERVAL_S:
            return
        self._last_scan = now
        try:
            ports = list_outputs()
        except Exception as e:
            log.warning(f"{self._label}: port scan failed: {e}")
            return
        idx = find_port(ports, self._fragment)
        new_name = ports[idx] if idx is not None else None

        if self._out is not None and new_name == self._port_name:
            if alsa_target_connected(new_name, 'Connected From'):
                return
            self._disconnect_locked('ALSA subscription was lost')
        if self._out is not None:
            self._disconnect_locked('port disappeared or re-enumerated')
        if idx is None:
            return

        out = None
        try:
            out = rtmidi.MidiOut()
            out.open_port(idx)
        except Exception as e:
            if out is not None:
                try:
                    out.close_port()
                except Exception:
                    pass
                try:
                    out.delete()
                except Exception:
                    pass
            log.warning(f"{self._label}: open failed for {new_name}: {e}")
            return
        self._out = out
        self._port_name = new_name
        log.info(f"{self._label}: connected {new_name}")

    def _monitor_ports(self):
        while not self._stop.wait(PORT_SCAN_INTERVAL_S):
            with self._lock:
                self._refresh_locked(force=True)

    def send_message(self, data):
        with self._lock:
            self._refresh_locked()
            for attempt in range(2):
                if self._out is None:
                    return False
                try:
                    self._out.send_message(data)
                    return True
                except Exception as e:
                    log.warning(f"{self._label}: send failed ({e}); reconnecting")
                    self._disconnect_locked()
                    if attempt == 0:
                        self._refresh_locked(force=True)
            return False

    def close_port(self):
        self._stop.set()
        with self._lock:
            self._disconnect_locked()


class DynamicMidiIn:
    """Polling MIDI input that reopens after USB/service re-enumeration."""
    def __init__(self, fragment, label):
        self._fragment = fragment
        self._label = label
        self._in = None
        self._port_name = None
        self._last_scan = 0.0
        self._refresh(force=True)

    def _disconnect(self, reason=None):
        old_name = self._port_name
        if self._in is not None:
            try:
                self._in.close_port()
            except Exception:
                pass
            try:
                self._in.delete()
            except Exception:
                pass
        self._in = None
        self._port_name = None
        if reason and old_name:
            log.warning(f"{self._label}: disconnected ({reason}); waiting for device")

    def _refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_scan < PORT_SCAN_INTERVAL_S:
            return
        self._last_scan = now
        try:
            ports = list_inputs()
        except Exception as e:
            log.warning(f"{self._label}: port scan failed: {e}")
            return
        idx = find_port(ports, self._fragment)
        new_name = ports[idx] if idx is not None else None

        if self._in is not None and new_name == self._port_name:
            if alsa_target_connected(new_name, 'Connecting To'):
                return
            self._disconnect('ALSA subscription was lost')
        if self._in is not None:
            self._disconnect('port disappeared or re-enumerated')
        if idx is None:
            return

        midi_in = None
        try:
            midi_in = rtmidi.MidiIn()
            midi_in.open_port(idx)
            midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        except Exception as e:
            if midi_in is not None:
                try:
                    midi_in.close_port()
                except Exception:
                    pass
                try:
                    midi_in.delete()
                except Exception:
                    pass
            log.warning(f"{self._label}: open failed for {new_name}: {e}")
            return
        self._in = midi_in
        self._port_name = new_name
        log.info(f"{self._label}: connected {new_name}")

    def get_message(self):
        self._refresh()
        if self._in is None:
            return None
        try:
            return self._in.get_message()
        except Exception as e:
            log.warning(f"{self._label}: read failed ({e}); reconnecting")
            self._disconnect()
            self._refresh(force=True)
            return None

    def close_port(self):
        self._disconnect()


class RawMidiOut:
    """Write raw MIDI bytes directly to rawmidi device (Linux/ringo only).

    VirMIDI's rawmidi loopback works; its ALSA sequencer path does not.
    QLC+ reads from VirMIDI via ALSA sequencer subscription, so writing
    here (rawmidi playback → loopback → capture → QLC+ sequencer sub) works.
    """
    def __init__(self, path):
        self._f = open(path, 'wb')
        log.info(f"qlc: {path}")

    def send_message(self, data):
        self._f.write(bytes(data))
        self._f.flush()

    def close(self):
        self._f.close()


class LoopMidiOut:
    """QLC+ bridge via loopMIDI (Windows/Constellation only) -- replaces
    the Linux rawmidi loopback, which has no Windows equivalent."""
    def __init__(self, port_fragment):
        self._out = rtmidi.MidiOut()
        ports = self._out.get_ports()
        idx = find_port(ports, port_fragment)
        if idx is None:
            raise RuntimeError(
                f"loopMIDI port '{port_fragment}' not found.\n"
                f"Available outputs: {ports}\n"
                f"Create/select it in loopMIDI, then restart."
            )
        self._out.open_port(idx)
        log.info(f"QLC bridge: {ports[idx]}")

    def send_message(self, data):
        self._out.send_message(list(data))

    def close(self):
        self._out.close_port()


def open_qlc_bridge():
    if IS_WINDOWS:
        return LoopMidiOut(PORT_QLC_BRIDGE)
    return RawMidiOut(QLC_RAWMIDI)


# ---------------------------------------------------------------------------
# MIDI send helpers
# ---------------------------------------------------------------------------

def send_pc(out, ch, prog):
    out.send_message([0xC0 | (ch - 1), prog & 0x7F])

def send_cc(out, ch, ctrl, val):
    out.send_message([0xB0 | (ch - 1), ctrl & 0x7F, val & 0x7F])

def send_note_on(out, ch, note, vel=127):
    out.send_message([0x90 | (ch - 1), note & 0x7F, vel & 0x7F])

def send_note_off(out, ch, note):
    out.send_message([0x80 | (ch - 1), note & 0x7F, 0])

def send_mox_master(mox_out, pp):
    """Select MOX6 Master slot via Yamaha SysEx parameter change.

    F0 43 1n 7F 14 [aH] [aM] [aL] [data] F7
    Model ID = 7F 14 (2 bytes), Address = 0A 00 00 (Master Number Select), no checksum.
    Note: Data List detail shows 1C but transmit/receive flow diagrams show 14 -- 14 is correct.
    MOX6 must have: Utility > F6 MIDI > SF2 SWITCH > RcvBulk = ON
    """
    slot = pp & 0x7F
    mox_out.send_message([0xF0, 0x43, 0x10, 0x7F, 0x14, 0x0A, 0x00, 0x00, slot, 0xF7])
    log.info(f"MOX6 Master -> slot {pp}")

def setup_qlc_midi():
    """Establish persistent QLC+ WebSocket and configure IO.

    Retries for up to 25s on cold boot. Called again automatically if
    the connection is ever lost.
    """
    global _qlc_ws
    deadline = time.time() + 25
    while time.time() < deadline:
        with _qlc_lock:
            if _qlc_ws is None:
                _qlc_ws = _qlc_connect()
            if _qlc_ws is not None:
                return
        time.sleep(1)
    log.warning("QLC+ WebSocket setup: QLC+ did not respond within 25s")


# ---------------------------------------------------------------------------
# QLC+ WebSocket helpers
# ---------------------------------------------------------------------------

_qlc_ws   = None
_qlc_lock = threading.Lock()

def _qlc_connect():
    """Open a fresh QLC+ WebSocket. IO is left as-is from the workspace config.
    Sending QLC+IO commands here was found to break the DMX USB output plugin."""
    import websocket as _wsmod
    try:
        ws = _wsmod.create_connection("ws://localhost:9999/qlcplusWS", timeout=0.4)
        log.info("QLC+ WebSocket connected")
        return ws
    except Exception as e:
        log.warning(f"QLC+ WS connect failed: {e}")
        return None

def _qlc_send(messages):
    """Send messages over persistent QLC+ WS; reconnects once on failure."""
    global _qlc_ws
    if not _qlc_lock.acquire(timeout=3.0):
        log.warning("_qlc_send: lock timeout, dropping messages")
        return
    try:
        for attempt in range(2):
            if _qlc_ws is None:
                _qlc_ws = _qlc_connect()
            if _qlc_ws is None:
                return
            try:
                for msg in messages:
                    _qlc_ws.send(msg)
                return
            except Exception as e:
                log.warning(f"QLC+ WS send failed (attempt {attempt+1}): {e}")
                try:
                    _qlc_ws.close()
                except Exception:
                    pass
                _qlc_ws = None
    finally:
        _qlc_lock.release()


# ---------------------------------------------------------------------------
# QLC+ native function control
# ---------------------------------------------------------------------------

def qlc_fn_start(fn_id):
    """Start a QLC+ function by ID."""
    global _current_fn_id
    _qlc_send([f"QLC+API|setFunctionStatus|{fn_id}|1"])
    _current_fn_id = fn_id
    log.info(f"fn start: {fn_id}")

def qlc_fn_stop(fn_id):
    """Stop a specific QLC+ function by ID."""
    _qlc_send([f"QLC+API|setFunctionStatus|{fn_id}|0"])
    log.info(f"fn stop:  {fn_id}")

def qlc_fn_stop_current():
    """Stop whatever function is currently running."""
    global _current_fn_id
    if _current_fn_id is not None:
        qlc_fn_stop(_current_fn_id)
        _current_fn_id = None

def qlc_fn_start_note(note):
    """Start the QLC+ function mapped to MIDI note. No-op if unmapped."""
    fn_id = NOTE_FN.get(note)
    if fn_id is None:
        log.warning(f"no function mapped to note {note}")
        return
    qlc_fn_start(fn_id)

def qlc_fn_switch(fn_id):
    """Stop current function and start fn_id in one WS batch — no gap between them."""
    global _current_fn_id
    msgs = []
    old = _current_fn_id
    if old is not None:
        msgs.append(f"QLC+API|setFunctionStatus|{old}|0")
    msgs.append(f"QLC+API|setFunctionStatus|{fn_id}|1")
    _qlc_send(msgs)
    _current_fn_id = fn_id
    log.info(f"fn switch {old} -> {fn_id}")

def qlc_fn_switch_note(note):
    """Like qlc_fn_switch but resolves fn_id from MIDI note."""
    fn_id = NOTE_FN.get(note)
    if fn_id is None:
        log.warning(f"no fn for note {note}")
        return
    qlc_fn_switch(fn_id)

def qlc_stomp_mirror(qlc, rr):
    """Mirror Stomp PC + CC 69 value=0 (100ms delay) to QLC+ -- matches Bome Outgoing2/3."""
    send_pc(qlc, 1, rr)
    def _cc():
        time.sleep(0.100)
        send_cc(qlc, 1, 69, 0)
    threading.Thread(target=_cc, daemon=True).start()


# ---------------------------------------------------------------------------
# Song PC handler
# ---------------------------------------------------------------------------

def process_pc(pp, hxstomp, ve500, qlc, mox_out=None):
    """Route a song PC to all devices.

    mox_out: if provided (BandHelper/OSC trigger), also send PC to MOX6 to sync patch.
             if None (MOX6 trigger), skip MOX6 to avoid an echo loop.
    """
    global current_mood, current_scenes, current_chases, ga, gb

    name, mood, rr, ve_pc = SONGS.get(pp, DEFAULT_SONG)
    src = 'BandHelper' if mox_out is not None else 'MOX6'
    log.info(f"[{src}] pp={pp:3d}  '{name}'  rr={rr}  ve={ve_pc}  mood={mood}")

    current_mood = mood
    ls_notes, lc_notes = LIGHTING[mood]
    current_scenes = ls_notes if ls_notes is not None else lc_notes
    current_chases = lc_notes
    ga = len(current_scenes) - 1  # first NEXT_SCENE plays index 0
    gb = len(current_chases) - 1  # first NEXT_CHASE plays index 0

    # HX Stomp
    send_pc(hxstomp, 1, rr)

    # VE-500
    send_pc(ve500, 1, ve_pc)

    # Sync MOX6 Master slot when triggered from BandHelper/OSC
    if mox_out is not None:
        send_mox_master(mox_out, pp)

    # Mirror Stomp PC + CC 69 value=0 to QLC+
    qlc_stomp_mirror(qlc, rr)


# ---------------------------------------------------------------------------
# Lighting note handlers (Bome Preset.0)
# ---------------------------------------------------------------------------

def process_note_on(note, velocity):
    global ga, gb

    if note == NOTE_PREV_SCENE:
        ga = (ga - 1) % len(current_scenes)
        qlc_fn_switch_note(current_scenes[ga])
        log.info(f"PREV SCENE -> note {current_scenes[ga]}")

    elif note == NOTE_NEXT_SCENE:
        ga = (ga + 1) % len(current_scenes)
        qlc_fn_switch_note(current_scenes[ga])
        log.info(f"NEXT SCENE -> note {current_scenes[ga]}")

    elif note == NOTE_SCENE_CHORD:
        fn_id = QLC_FN_BASE_SCENE if velocity > VELOCITY_HARD else QLC_FN_ALL_SCENE_CHASER
        qlc_fn_switch(fn_id)
        log.info(f"SCENE CHORD vel={velocity} -> fn {fn_id}")

    elif note == NOTE_CHASE_CHORD:
        fn_id = QLC_FN_STROBE if velocity > VELOCITY_HARD else QLC_FN_ALL_CHASERS
        qlc_fn_switch(fn_id)
        log.info(f"CHASE CHORD vel={velocity} -> fn {fn_id}")

    elif note == NOTE_PREV_CHASE:
        gb = (gb - 1) % len(current_chases)
        qlc_fn_switch_note(current_chases[gb])
        log.info(f"PREV CHASE -> note {current_chases[gb]}")

    elif note == NOTE_NEXT_CHASE:
        gb = (gb + 1) % len(current_chases)
        qlc_fn_switch_note(current_chases[gb])
        log.info(f"NEXT CHASE -> note {current_chases[gb]}")

    elif note == NOTE_BLACKOUT:
        global _last_blackout_t
        now = time.time()
        if now - _last_blackout_t < BLACKOUT_DEBOUNCE_S:
            log.info("BLACKOUT debounced")
            return
        _last_blackout_t = now
        if _current_fn_id == QLC_FN_BLACKOUT:
            qlc_fn_switch_note(current_chases[gb])
            log.info("BLACKOUT OFF")
        else:
            qlc_fn_switch(QLC_FN_BLACKOUT)
            log.info("BLACKOUT ON")


# ---------------------------------------------------------------------------
# Note event worker -- processes queued MIDI events so the poll loop never blocks
# ---------------------------------------------------------------------------

def _note_worker(qlc, hxstomp, ve500, mox_out):
    while True:
        ev = _note_q.get()
        try:
            etype = ev[0]
            if etype == 'note':
                process_note_on(ev[1], ev[2])
            elif etype == 'pc':
                process_pc(ev[1], hxstomp, ve500, qlc, ev[2])
        except Exception as e:
            log.error(f"note worker error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    host = platform.node()
    log.info(f"MIDI brain starting on {host} ({'Windows' if IS_WINDOWS else 'Linux'})")

    in_ports = list_inputs()
    log.info(f"MIDI inputs: {in_ports}")
    if IS_WINDOWS:
        log.info(f"MIDI outputs: {list_outputs()}")

    # --- Outputs (opened first so OSC handler can reference them) ---
    hxstomp = LazyMidiOut(PORT_HXSTOMP, 'hxstomp')
    ve500   = LazyMidiOut(PORT_VE500,   've500')
    qlc     = open_qlc_bridge()
    mox_out = LazyMidiOut(PORT_MOX6,    'mox_out')  # to sync MOX6 from BandHelper/OSC

    global NOTE_FN
    NOTE_FN = _parse_note_fn()
    threading.Thread(target=setup_qlc_midi, daemon=True).start()
    threading.Thread(target=_note_worker, args=(qlc, hxstomp, ve500, mox_out),
                     daemon=True, name='note-worker').start()

    # --- OSC server: BandHelper sends /pc <int> to port 9000 ---
    def _osc_pc(address, *args):
        if args:
            try:
                pp = int(args[0])
                log.info(f"[OSC] /pc {pp}")
                process_pc(pp, hxstomp, ve500, qlc, mox_out)
            except Exception as e:
                log.error(f"OSC handler error: {e}")

    dispatcher = Dispatcher()
    dispatcher.map("/pc", _osc_pc)
    osc_server = ThreadingOSCUDPServer(("0.0.0.0", OSC_PORT), dispatcher)
    osc_thread = threading.Thread(target=osc_server.serve_forever, daemon=True, name="osc")
    osc_thread.start()
    log.info(f"OSC server listening on :{OSC_PORT}")

    # Inputs continuously follow device/service re-enumeration. The brain can
    # start with either source absent and picks it up whenever it appears.
    midi_in_mox = DynamicMidiIn(PORT_MOX6, 'MOX6 in')
    midi_in_bh = DynamicMidiIn(PORT_BANDHELPER, 'BandHelper in')

    log.info("Brain running.")
    try:
        while True:
            # MOX6 → route everywhere except back to MOX6
            msg = midi_in_mox.get_message()
            if msg:
                data, _ = msg
                if data:
                    status  = data[0] & 0xF0
                    channel = (data[0] & 0x0F) + 1
                    if status == 0xC0 and channel == 1 and len(data) >= 2:
                        try: _note_q.put_nowait(('pc', data[1], None))
                        except _queue.Full: log.warning("note queue full, dropping MOX6 PC")
                    elif status == 0x90 and len(data) >= 3 and data[2] > 0:
                        try: _note_q.put_nowait(('note', data[1], data[2]))
                        except _queue.Full: log.warning("note queue full, dropping note")

            # BandHelper RTP MIDI → route everywhere including MOX6
            msg = midi_in_bh.get_message()
            if msg:
                data, _ = msg
                if data:
                    status  = data[0] & 0xF0
                    channel = (data[0] & 0x0F) + 1
                    if channel == 1 and status == 0xC0 and len(data) >= 2:
                        try: _note_q.put_nowait(('pc', data[1], mox_out))
                        except _queue.Full: log.warning("note queue full, dropping BH PC")

            time.sleep(0.001)

    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        osc_server.shutdown()
        midi_in_mox.close_port()
        midi_in_bh.close_port()
        hxstomp.close_port()
        ve500.close_port()
        qlc.close()
        mox_out.close_port()


if __name__ == '__main__':
    main()
