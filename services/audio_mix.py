import numpy as np
from typing import List
from pathlib import Path
import wave


def mix_wav_files(input_paths: List[str], output_path: str, normalize: bool = True, target_duration_sec: float = 0.0) -> str:
    if not input_paths:
        return ""

    signals = []
    sample_rate = None
    max_frames = 0

    for path in input_paths:
        if not path:
            continue
        with wave.open(path, "rb") as wav:
            frames = wav.readframes(wav.getnframes())
            data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            if wav.getnchannels() > 1:
                data = data.reshape(-1, wav.getnchannels())
                data = data.mean(axis=1)
            if sample_rate is None:
                sample_rate = wav.getframerate()
            max_frames = max(max_frames, data.shape[0])
            signals.append(data)

    if not signals or sample_rate is None:
        return ""

    mix = np.zeros(max_frames, dtype=np.float32)
    for data in signals:
        if data.shape[0] < max_frames:
            data = np.pad(data, (0, max_frames - data.shape[0]))
        mix += data

    if normalize and np.max(np.abs(mix)) > 1.0:
        mix /= np.max(np.abs(mix))

    if target_duration_sec and sample_rate:
        target_frames = int(target_duration_sec * sample_rate)
        if target_frames > mix.shape[0]:
            mix = np.pad(mix, (0, target_frames - mix.shape[0]))
        elif target_frames < mix.shape[0]:
            mix = mix[:target_frames]

    mix_pcm = np.clip(mix, -1.0, 1.0)
    mix_pcm = (mix_pcm * 32767.0).astype(np.int16)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(output_path, "wb") as out_wav:
        out_wav.setnchannels(1)
        out_wav.setsampwidth(2)
        out_wav.setframerate(sample_rate)
        out_wav.writeframes(mix_pcm.tobytes())

    return output_path

