"""Synthesize the narration with Kokoro (open-weights TTS, Apache-2.0), locally, no API.
Usage: python demo/voice.py <kokoro-v1.0.onnx> <voices-v1.0.bin>  -> demo/voice/<scene>.wav + durations.json"""
import json
import sys
from pathlib import Path

import soundfile as sf
from kokoro_onnx import Kokoro

HERE = Path(__file__).resolve().parent
cfg = json.loads((HERE / "narration.json").read_text())
k = Kokoro(sys.argv[1], sys.argv[2])
out = HERE / "voice"
out.mkdir(exist_ok=True)
dur = {}
for name, text in cfg["scenes"].items():
    samples, rate = k.create(text, voice=cfg["voice"], speed=cfg["speed"], lang="en-us")
    sf.write(out / f"{name}.wav", samples, rate)
    dur[name] = round(len(samples) / rate, 2)
    print(name, dur[name], "s")
(out / "durations.json").write_text(json.dumps(dur, indent=1))
