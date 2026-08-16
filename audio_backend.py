"""
System-audio capture, abstracted over the platform.

Linux/macOS use sounddevice against the PulseAudio monitor source.  Windows
cannot: sounddevice 0.5.5 (the latest release) has no WASAPI loopback support,
so the Windows backend uses PyAudioWPatch — a PortAudio fork that does.

Both backends hand the detector 1-D float32 blocks of *exactly* ``blocksize``
frames, already down-mixed to mono.  WASAPI does not honour ``frames_per_buffer``
as strictly as ALSA, so the base class re-chunks whatever the driver delivers.
"""

import subprocess
import sys

import numpy as np

IS_WINDOWS = sys.platform == "win32"


# ----------------------------------------------------------------------
# Base
# ----------------------------------------------------------------------

class _BaseCapture:
    """Common re-chunking logic.  Subclasses set samplerate/channels/device_name."""

    samplerate: int = 48000
    channels: int = 1
    device_name: str = "unknown"

    def __init__(self, blocksize: int, on_block) -> None:
        self.blocksize = blocksize
        self._on_block = on_block
        self._residual = np.zeros(0, dtype="float32")

    def _emit(self, mono: np.ndarray) -> None:
        """Split *mono* into exact-blocksize chunks, carrying the remainder over."""
        if self._residual.size:
            mono = np.concatenate((self._residual, mono))
        n = self.blocksize
        count = mono.size // n
        for i in range(count):
            self._on_block(mono[i * n:(i + 1) * n])
        self._residual = np.array(mono[count * n:], dtype="float32")

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


# ----------------------------------------------------------------------
# Linux / macOS — sounddevice
# ----------------------------------------------------------------------

class SoundDeviceCapture(_BaseCapture):

    def __init__(self, device, blocksize: int, on_block) -> None:
        super().__init__(blocksize, on_block)
        import sounddevice as sd
        self._sd = sd
        self.device = device
        try:
            info = sd.query_devices(device if device is not None
                                    else sd.default.device[0])
            self.samplerate = int(info["default_samplerate"])
            self.channels = min(2, max(1, int(info["max_input_channels"])))
            self.device_name = str(info["name"])
        except Exception:
            self.samplerate = 48000
            self.channels = 1
            self.device_name = "default"
        self._stream = None

    def start(self) -> None:
        self._stream = self._sd.InputStream(
            device=self.device,
            channels=self.channels,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=self._cb,
            latency="low",
        )
        self._stream.start()

    def _cb(self, indata, frames, time_info, status) -> None:
        mono = indata.mean(axis=1) if self.channels > 1 else indata[:, 0]
        self._emit(np.asarray(mono, dtype="float32"))

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


# ----------------------------------------------------------------------
# Windows — WASAPI loopback via PyAudioWPatch
# ----------------------------------------------------------------------

def _default_loopback_info(pa, pyaudio):
    """The loopback device matching the current default output device."""
    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if speakers.get("isLoopbackDevice", False):
        return speakers
    for loopback in pa.get_loopback_device_info_generator():
        if speakers["name"] in loopback["name"]:
            return loopback
    raise RuntimeError(
        f"No WASAPI loopback device found for default output "
        f"'{speakers['name']}'. Run with --list-devices to see what is available."
    )


class WasapiLoopbackCapture(_BaseCapture):

    def __init__(self, device, blocksize: int, on_block) -> None:
        super().__init__(blocksize, on_block)
        import pyaudiowpatch as pyaudio
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()

        if device is None:
            info = _default_loopback_info(self._pa, pyaudio)
        else:
            info = self._pa.get_device_info_by_index(int(device))

        self._index = int(info["index"])
        self.channels = max(1, int(info["maxInputChannels"]))
        self.samplerate = int(info["defaultSampleRate"])
        self.device_name = str(info["name"])
        self._stream = None

    def start(self) -> None:
        self._stream = self._pa.open(
            format=self._pyaudio.paFloat32,
            channels=self.channels,
            rate=self.samplerate,
            frames_per_buffer=self.blocksize,
            input=True,
            input_device_index=self._index,
            stream_callback=self._cb,
            start=False,
        )
        self._stream.start_stream()

    def _cb(self, in_data, frame_count, time_info, status):
        data = np.frombuffer(in_data, dtype=np.float32)
        if self.channels > 1:
            data = data.reshape(-1, self.channels).mean(axis=1)
        self._emit(np.asarray(data, dtype="float32"))
        return (None, self._pyaudio.paContinue)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None


def create_capture(device, blocksize: int, on_block) -> _BaseCapture:
    if IS_WINDOWS:
        return WasapiLoopbackCapture(device, blocksize, on_block)
    return SoundDeviceCapture(device, blocksize, on_block)


# ----------------------------------------------------------------------
# Device discovery
# ----------------------------------------------------------------------

def list_input_devices() -> None:
    if IS_WINDOWS:
        _list_windows_devices()
    else:
        _list_unix_devices()


def _list_unix_devices() -> None:
    import sounddevice as sd
    print("\nAvailable input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"  {i:2d}: {d['name']}")
    print()
    print("For system audio capture, use --device pulse (or --device pipewire)")
    print("and ensure your monitor source is the default PulseAudio input.")
    print("Run: pactl set-default-source <monitor-source-name>")
    print("  or use pavucontrol → Input Devices → set the correct monitor as default.")


def _list_windows_devices() -> None:
    import pyaudiowpatch as pyaudio
    pa = pyaudio.PyAudio()
    try:
        print("\nWASAPI loopback devices (these capture system audio):")
        try:
            for d in pa.get_loopback_device_info_generator():
                print(f"  {d['index']:2d}: {d['name']}")
        except Exception as exc:
            print(f"  (none available: {exc})")
        print("\nOther input devices (microphones etc.):")
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice", False):
                print(f"  {i:2d}: {d['name']}")
        print()
        print("Leave --device unset to capture the current default output device.")
    finally:
        pa.terminate()


def find_device_by_hint(hint: str) -> int | None:
    """Accept a device index (int string) or a name substring."""
    try:
        return int(hint)
    except ValueError:
        pass

    if IS_WINDOWS:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        try:
            for d in pa.get_loopback_device_info_generator():
                if hint.lower() in d["name"].lower():
                    return int(d["index"])
            for i in range(pa.get_device_count()):
                d = pa.get_device_info_by_index(i)
                if d["maxInputChannels"] > 0 and hint.lower() in d["name"].lower():
                    return i
        finally:
            pa.terminate()
        return None

    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if hint.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    return None


def auto_pick_device() -> int | None:
    """
    Pick the system-audio device.  On Windows, None means 'default output
    loopback', which the capture backend resolves itself.
    """
    if IS_WINDOWS:
        return None
    import sounddevice as sd
    devices = sd.query_devices()
    for keyword in ("pulse", "pipewire", "default"):
        for i, d in enumerate(devices):
            if d["name"].lower() == keyword and d["max_input_channels"] > 0:
                return i
    return None


def configure_monitor_source() -> str | None:
    """
    Linux: find the RUNNING monitor source via pactl and make it the default
    input, so that 'pulse' captures system audio.  Returns the source name.

    Windows: no-op — WASAPI loopback needs no system-wide reconfiguration.
    """
    if IS_WINDOWS:
        return None
    try:
        r = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True, text=True, timeout=3,
        )
        for line in r.stdout.splitlines():
            parts = line.split()
            # fields: idx  name  driver  format  state  (state is last column)
            if len(parts) >= 3 and "monitor" in parts[1] and parts[-1] == "RUNNING":
                name = parts[1]
                subprocess.run(
                    ["pactl", "set-default-source", name],
                    capture_output=True, timeout=3,
                )
                return name
    except Exception:
        pass
    return None
