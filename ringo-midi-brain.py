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

VE-500 patch PC values (0-indexed):
  U01 TZ BASE=0  U02 WET=1  U03 3PT UP=2  U04 3PT WRAP=3
  U05 SHOUT=4    U06 RADIO=5  U07 LOW OCT=6  U08 LONG DLY=7

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
#   ve_pc = VE-500 PC (0=U01 TZ BASE ... 7=U08 LONG DLY)
# ---------------------------------------------------------------------------
def _song(lighting, vox_pc, rr, name):
    return (name, lighting, rr, vox_pc)

R  = 116  # Regular
CL = 117  # Clean
MT = 118  # Metal

# vox PC shortcuts
BASE = 0   # U01 TZ BASE
WET  = 1   # U02 WET
UP3  = 2   # U03 3PT UP
WRAP = 3   # U04 3PT WRAP
SHOUT= 4   # U05 SHOUT
LOCT = 6   # U07 LOW OCT
LDLY = 7   # U08 LONG DLY

SONGS = {
    1:  _song('High Energy', BASE, R,  'Rebel Yell'),
    2:  _song('High Energy', BASE, R,  'Rebel Yell'),
    3:  _song('Default',     BASE, R,  'Short Skirt Long Jacket'),
    4:  _song('Floyd',       LDLY, R,  'Comfortably Numb'),
    5:  _song('Default',     BASE, R,  'The Distance'),
    6:  _song('Default',     BASE, R,  'I Will Survive'),
    7:  _song('High Energy', BASE, R,  'Boys of Summer'),
    8:  _song('Floyd',       BASE, R,  'Have A Cigar'),
    # pp=9: LA Woman / Love Me Two Times share this slot -- same settings
    9:  _song('Atmospheric', BASE, R,  'LA Woman / Love Me Two Times'),
    10: _song('Atmospheric', WET,  R,  'Lost Together'),
    11: _song('Atmospheric', WET,  R,  'Lovesong'),
    12: _song('High Energy', BASE, R,  'Pump It Up'),
    13: _song('Default',     BASE, R,  'White Wedding'),
    14: _song('Default',     BASE, R,  'Bad Guy'),
    16: _song('Floyd',       LDLY, R,  'Time'),
    17: _song('Floyd',       LDLY, R,  'Time'),
    18: _song('Floyd',       LDLY, R,  'Time'),
    32: _song('Default',     UP3,  CL, 'American Girl'),
    33: _song('Default',     BASE, R,  'Are You Gonna Be My Girl'),
    34: _song('Default',     WRAP, R,  'Banditos'),
    35: _song('Default',     WET,  R,  'Brown Sugar'),
    36: _song('Default',     BASE, R,  'Courage'),
    37: _song('Default',     BASE, R,  'Fifty Mission Cap'),
    38: _song('Default',     BASE, R,  'Flagpole Sitta'),
    39: _song('Default',     BASE, R,  'Fortunate Son'),
    40: _song('Default',     WRAP, CL, 'Here Comes My Girl'),
    41: _song('Default',     BASE, R,  'Hey Jealousy'),
    42: _song('Default',     LOCT, R,  'La Grange'),
    43: _song('Default',     BASE, R,  'Little Bones'),
    44: _song('Default',     BASE, CL, "Runnin' Down A Dream"),
    45: _song('Default',     BASE, CL, 'Santeria'),
    46: _song('Default',     BASE, R,  'Take Me Out'),
    47: _song('Default',     UP3,  R,  'The Boys Are Back In Town'),
    48: _song('Default',     BASE, R,  'The Middle'),
    49: _song('Default',     BASE, R,  'Three Pistols'),
    50: _song('Default',     UP3,  R,  'You May Be Right'),
    51: _song('Default',     BASE, R,  'You Wreck Me'),
    52: _song('High Energy', BASE, R,  'All The Small Things'),
    53: _song('High Energy', BASE, MT, 'Basket Case'),
    54: _song('High Energy', SHOUT,R,  'Blitzkrieg Bop'),
    55: _song('High Energy', BASE, R,  'Breaking The Law'),
    56: _song('High Energy', SHOUT,R,  'Fight For Your Right To Party'),
    57: _song('High Energy', BASE, R,  'Flavor Of The Weak'),
    58: _song('High Energy', SHOUT,R,  'I Wanna Be Sedated'),
    59: _song('High Energy', BASE, R,  'Mrs Robinson'),
    60: _song('High Energy', SHOUT,MT, 'One Eyed Bastard'),
    61: _song('High Energy', BASE, R,  'Ring Of Fire'),
    62: _song('High Energy', BASE, MT, 'Seek And Destroy'),
    63: _song('High Energy', SHOUT,R,  'The Rock Show'),
    64: _song('High Energy', BASE, MT, 'Welcome To Paradise'),
    65: _song('High Energy', BASE, R,  "What's My Age Again"),
    66: _song('High Energy', WRAP, R,  'Young Lust'),
    67: _song('Atmospheric', BASE, R,  'Red House'),
    68: _song('Atmospheric', WET,  R,  'Roadhouse Blues'),
    69: _song('Purple Rain', LDLY, R,  'Purple Rain'),
}

DEFAULT_SONG = _song('Default', BASE, R, '(unknown)')

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

def list_inputs():
    tmp = rtmidi.MidiIn()
    p = tmp.get_ports()
    del tmp
    return p


def list_outputs():
    tmp = rtmidi.MidiOut()
    p = tmp.get_ports()
    del tmp
    return p


def find_port(ports, fragment):
    fl = fragment.lower()
    for i, p in enumerate(ports):
        if fl in p.lower():
            return i
    return None


class DummyOut:
    """Stand-in for hardware that isn't connected. Windows' MMAPI backend
    can't open virtual ports (unlike ALSA on ringo), so just drop messages."""
    def send_message(self, data):
        pass

    def close_port(self):
        pass


class LazyMidiOut:
    """MIDI output that auto-connects when the device appears.

    If the target port isn't found at construction time, silently drops
    messages and retries port discovery every 5 seconds on the next send.
    Works on both Linux (no virtual-port fallback needed) and Windows.
    """
    def __init__(self, fragment, label):
        self._fragment = fragment
        self._label = label
        self._out = None
        self._last_scan = 0.0
        self._lock = threading.Lock()
        self._try_connect()

    def _try_connect(self):
        ports = list_outputs()
        idx = find_port(ports, self._fragment)
        if idx is not None:
            out = rtmidi.MidiOut()
            out.open_port(idx)
            self._out = out
            log.info(f"{self._label}: {ports[idx]}")

    def send_message(self, data):
        with self._lock:
            if self._out is None:
                now = time.time()
                if now - self._last_scan >= 5.0:
                    self._last_scan = now
                    self._try_connect()
            if self._out is not None:
                try:
                    self._out.send_message(data)
                except Exception as e:
                    log.warning(f"{self._label}: send failed ({e}), will reconnect")
                    try:
                        self._out.close_port()
                    except Exception:
                        pass
                    self._out = None

    def close_port(self):
        with self._lock:
            if self._out is not None:
                self._out.close_port()
                self._out = None


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

    # --- MOX6 input (optional -- brain runs without it for BandHelper-only use) ---
    midi_in_mox = None
    idx_mox = find_port(in_ports, PORT_MOX6)
    if idx_mox is not None:
        midi_in_mox = rtmidi.MidiIn()
        midi_in_mox.open_port(idx_mox)
        midi_in_mox.ignore_types(sysex=True, timing=True, active_sense=True)
        log.info(f"MOX6 in: {in_ports[idx_mox]}")
    else:
        log.warning(f"MOX6 not found ('{PORT_MOX6}') -- MOX6 input disabled")

    # --- BandHelper input via rtpMIDI/rtpmidid (retry for startup) ---
    midi_in_bh = None
    for _attempt in range(BANDHELPER_RETRY_S):
        _ports = list_inputs()
        idx_bh = find_port(_ports, PORT_BANDHELPER)
        if idx_bh is not None:
            midi_in_bh = rtmidi.MidiIn()
            midi_in_bh.open_port(idx_bh)
            midi_in_bh.ignore_types(sysex=True, timing=True, active_sense=True)
            log.info(f"BandHelper in: {_ports[idx_bh]}")
            break
        log.info(f"Waiting for '{PORT_BANDHELPER}'... ({_attempt+1}/{BANDHELPER_RETRY_S})")
        time.sleep(1)
    else:
        log.warning(f"'{PORT_BANDHELPER}' not found after {BANDHELPER_RETRY_S}s -- BandHelper disabled")

    log.info("Brain running.")
    try:
        while True:
            # MOX6 → route everywhere except back to MOX6
            msg = midi_in_mox.get_message() if midi_in_mox else None
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
            if midi_in_bh:
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
        if midi_in_mox:
            midi_in_mox.close_port()
            del midi_in_mox
        if midi_in_bh:
            midi_in_bh.close_port()
            del midi_in_bh
        hxstomp.close_port()
        ve500.close_port()
        qlc.close()
        mox_out.close_port()


if __name__ == '__main__':
    main()
