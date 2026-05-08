from pathlib import Path
import subprocess
from typing import Optional


def mux_audio_video(video_path: str, audio_path: str, output_path: Optional[str] = None) -> str:
    if not video_path or not audio_path:
        return ""

    input_video = Path(video_path)
    input_audio = Path(audio_path)
    if output_path is None:
        output_path = str(input_video.with_name(f"{input_video.stem}_av{input_video.suffix}"))

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_video),
        "-i", str(input_audio),
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]

    subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return output_path

