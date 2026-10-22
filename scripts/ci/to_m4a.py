"""CI helper: re-encode a WAV file as AAC in an MP4 container, the way an iPhone records.

python scripts/ci/to_m4a.py in.wav out.m4a
"""

from __future__ import annotations

import sys

import av

src, dst = sys.argv[1], sys.argv[2]
with av.open(src) as inp, av.open(dst, "w", format="mp4") as out:
    stream = out.add_stream("aac", rate=48000, layout="mono")
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=48000)
    for frame in inp.decode(audio=0):
        for f in resampler.resample(frame):
            for packet in stream.encode(f):
                out.mux(packet)
    for f in resampler.resample(None):
        for packet in stream.encode(f):
            out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
