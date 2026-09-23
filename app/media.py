"""Streams a video from the file server to the browser: as it is (with HTTP range support), or converted on the fly to a small
H.264/AAC fragmented MP4 for formats a browser cannot decode (WMV, MPEG-1, old MPEG-4 AVI, ...)."""
import os
import queue
import re
import sys
import threading
import time
from fractions import Fraction

MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".mkv": "video/x-matroska",
        ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".flv": "video/x-flv"}
NATIVE_CODECS = {"h264", "vp8", "vp9", "av1"}
NATIVE_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
_slots = threading.BoundedSemaphore(2)  # converting is CPU heavy: at most two streams at once


class _Stream:
    """One open stream of a file. The app can close its file handle from outside, because a browser that drops the connection leaves the
    generator suspended and Python may take a long time to clean it up, which keeps the file locked on the share (moves and deletes fail)."""

    def __init__(self, unc):
        self.unc, self.ev, self.closers, self.t = unc, threading.Event(), [], time.time()

    def close(self):
        self.ev.set()
        for fn in self.closers:
            try:
                fn()
            except Exception:
                pass


_streams = set()
_streams_lock = threading.Lock()
IDLE_CLOSE = 900  # seconds: a stream nobody has read from for 15 minutes is abandoned


def _open_stream(unc):
    st = _Stream(unc)
    with _streams_lock:
        _streams.add(st)
    return st


def _drop(st):
    with _streams_lock:
        _streams.discard(st)


def release(uncs, wait=5.0):
    """Close every stream that reads one of these files, right now. Called before a file is moved or deleted, and when the player leaves a pair."""
    want = {u.lower() for u in uncs}
    with _streams_lock:
        hit = [st for st in _streams if st.unc.lower() in want]
    for st in hit:
        st.close()
        _drop(st)
    if hit:
        time.sleep(0.4)  # let the SMB close settle before the caller moves or deletes
    return bool(hit)


def release_all(wait=5.0):
    with _streams_lock:
        uncs = [st.unc for st in _streams]
    return release(uncs, wait)


def _reaper():
    while True:
        time.sleep(30)
        with _streams_lock:
            old = [st for st in _streams if time.time() - st.t > IDLE_CLOSE]
        for st in old:
            st.close()
            _drop(st)


threading.Thread(target=_reaper, daemon=True).start()


def is_native(path, codec):
    """Whether a browser can probably play the file as it is. An unknown codec is tried natively; the player falls back on error."""
    return os.path.splitext(path)[1].lower() in NATIVE_EXT and (codec is None or codec in NATIVE_CODECS)


def parse_range(header, size):
    """-> (status, start, end) for a single 'bytes=a-b' range; raises ValueError when it cannot be satisfied."""
    if not header:
        return 200, 0, size - 1
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (m[1] == "" and m[2] == ""):
        raise ValueError("bad range")
    if m[1] == "":  # suffix: the last N bytes
        start, end = max(size - int(m[2]), 0), size - 1
    else:
        start, end = int(m[1]), (int(m[2]) if m[2] else size - 1)
    end = min(end, size - 1)
    if start > end or start >= size:
        raise ValueError("unsatisfiable")
    return 206, start, end


def range_stream(unc, start, end, chunk=1 << 20):
    import smbclient
    st = _open_stream(unc)
    f = None
    try:
        # share_access: a browser fetches several ranges of one file at once, and the file may be open elsewhere (a second stream, a player)
        f = smbclient.open_file(unc, mode="rb", buffering=chunk, share_access="rw")
        st.closers.append(f.close)
        f.seek(start)
        left = end - start + 1
        while left > 0 and not st.ev.is_set():
            data = f.read(min(chunk, left))
            if not data:
                break
            st.t = time.time()
            left -= len(data)
            yield data
    except Exception:
        if not st.ev.is_set():  # after release() the file was closed under us on purpose
            raise
    finally:
        _drop(st)
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


class _Sink:
    """File-like output for PyAV that hands the muxed bytes to the HTTP response, with back-pressure."""

    def __init__(self, q, stop):
        self.q, self.stop = q, stop

    def write(self, b):
        data = bytes(b)
        while True:
            if self.stop.is_set():
                raise IOError("client went away")
            try:
                self.q.put(data, timeout=0.2)  # short, so a stream we just release()d notices and frees its slot quickly, not up to 1s later
                return len(data)
            except queue.Full:
                pass

    def flush(self):
        pass


def _convert(unc, start, sink, stop, max_h, st):
    import av
    import smbclient
    with smbclient.open_file(unc, mode="rb", buffering=1 << 20, share_access="rw") as f, av.open(f) as src:
        st.closers.append(f.close)
        vs = next(s for s in src.streams if s.type == "video")
        aud = next((s for s in src.streams if s.type == "audio"), None)
        vs.thread_type = "AUTO"
        fps = float(vs.average_rate or vs.guessed_rate or 25)
        rate = Fraction(fps if 5 <= fps <= 60 else 25).limit_denominator(1001)
        w, h = vs.codec_context.width, vs.codec_context.height
        scale = min(1.0, max_h / h)
        ow, oh = max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)
        out = av.open(sink, "w", format="mp4", options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
        ov = out.add_stream("libx264", rate=rate, options={"preset": "ultrafast", "crf": "28", "tune": "zerolatency"})
        ov.width, ov.height, ov.pix_fmt = ow, oh, "yuv420p"
        oa = resampler = None
        if aud is not None:
            oa = out.add_stream("aac", rate=44100)
            oa.layout = "stereo"
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=44100)
        if start > 0:
            src.seek(int(start * 1_000_000))
        last_v, a_next = -1, None
        for packet in src.demux(*([vs] + ([aud] if aud is not None else []))):
            if stop.is_set():
                return
            try:
                frames = packet.decode()
            except av.error.InvalidDataError:
                continue  # damaged packet: skip it
            for frame in frames:
                t = frame.time
                if t is None or t < start - 0.001:
                    continue  # after a keyframe seek, drop what lies before the requested position
                if packet.stream.type == "video":
                    frame = frame.reformat(width=ow, height=oh, format="yuv420p")
                    pts = max(int(round((t - start) * rate)), last_v + 1)
                    last_v, frame.pts, frame.time_base = pts, pts, Fraction(1, 1) / rate
                    for p in ov.encode(frame):
                        out.mux(p)
                else:
                    for rf in resampler.resample(frame):
                        if a_next is None:
                            a_next = int(round((t - start) * 44100))
                        rf.pts, rf.time_base = a_next, Fraction(1, 44100)
                        a_next += rf.samples
                        for p in oa.encode(rf):
                            out.mux(p)
        for p in ov.encode(None):
            out.mux(p)
        if oa is not None:
            for p in oa.encode(None):
                out.mux(p)
        out.close()


def convert_stream(unc, start=0.0, max_h=720):
    """Generator of fragmented-MP4 bytes; the position is 'start' seconds into the source. Returns None if both slots are busy."""
    if not _slots.acquire(blocking=False):
        return None
    st = _open_stream(unc)
    q, stop, done = queue.Queue(maxsize=24), st.ev, object()

    def run():
        try:
            _convert(unc, float(start), _Sink(q, stop), stop, max_h, st)
        except Exception as ex:
            if not stop.is_set():
                print(f"convert failed for {unc}: {ex!r}", file=sys.stderr, flush=True)
        finally:
            _drop(st)
            _slots.release()  # here, not in the generator: an abandoned generator would never give the slot back
            while not stop.is_set():
                try:
                    q.put(done, timeout=1)
                    break
                except queue.Full:
                    pass

    threading.Thread(target=run, daemon=True).start()

    def gen():
        try:
            while True:
                b = q.get()
                if b is done:
                    return
                st.t = time.time()
                yield b
        finally:
            stop.set()

    return gen()
