import shutil
import subprocess
import time
from pathlib import Path

from utils.logger import logger_service


logger = logger_service.get_logger()


def denoise_wav_with_deepfilter(
    input_wav: str | Path,
    output_dir: str | Path,
    enabled: bool = True,
) -> Path:
    """
    AI noise suppression for the final WAV file.

    Uses DeepFilterNet CLI:
        deepFilter input.wav --output-dir out_dir

    If DeepFilterNet is unavailable or fails, returns the original input_wav.
    """
    input_wav = Path(input_wav)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not enabled:
        logger.info("Denoise skipped: disabled")
        return input_wav

    if not input_wav.exists():
        logger.warning("Denoise skipped: input wav does not exist: %s", input_wav)
        return input_wav

    deepfilter_bin = shutil.which("deepFilter")
    if deepfilter_bin is None:
        logger.warning("Denoise skipped: deepFilter command not found")
        return input_wav

    work_dir = output_dir / "denoise_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    prepared_wav = work_dir / "audio_48k_mono.wav"
    prepare_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_wav),
        "-ac",
        "1",
        "-ar",
        "48000",
        str(prepared_wav),
    ]

    try:
        subprocess.run(
            prepare_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
    except Exception as exc:
        logger.warning("Denoise skipped: failed to prepare wav for DeepFilterNet: %s", exc)
        return input_wav

    before = time.time()
    cmd = [
        deepfilter_bin,
        str(prepared_wav),
        "--output-dir",
        str(work_dir),
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        logger.info("DeepFilterNet stdout: %s", result.stdout[-1000:])
        if result.stderr:
            logger.info("DeepFilterNet stderr: %s", result.stderr[-1000:])
    except Exception as exc:
        logger.warning("Denoise failed, using original audio: %s", exc)
        return input_wav

    candidates = [
        path
        for path in work_dir.glob("*.wav")
        if path.resolve() != prepared_wav.resolve()
        and path.stat().st_mtime >= before
    ]

    if not candidates:
        logger.warning("Denoise failed: DeepFilterNet produced no wav output")
        return input_wav

    denoised_wav = max(candidates, key=lambda path: path.stat().st_mtime)
    final_wav = output_dir / "audio_denoised.wav"

    try:
        shutil.copyfile(denoised_wav, final_wav)
    except Exception as exc:
        logger.warning("Denoise failed: cannot copy output wav: %s", exc)
        return input_wav

    logger.info("Denoised audio created: %s", final_wav)
    return final_wav
