"""Local CPU transcription. No paid service and no retained audio."""
import io
import threading
from pathlib import Path

import numpy as np

MODEL_DIR = Path.home() / 'AppData' / 'Local' / 'QQChatGPTPlanBridge' / 'speech-small'
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 600
_model = None
_lock = threading.Lock()


def transcribe(data: bytes) -> str:
    global _model
    if not data or len(data) > MAX_BYTES:
        raise ValueError('语音为空或超过 20 MB。')
    import av
    from faster_whisper import WhisperModel
    # Bound decoded duration before inference, including compressed input.
    chunks = []
    count = 0
    resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            for converted in resampler.resample(frame):
                chunk = converted.to_ndarray().reshape(-1)
                count += len(chunk)
                if count > MAX_SECONDS * 16000:
                    raise ValueError('单条语音最多 10 分钟。')
                chunks.append(chunk)
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError('没有可识别的音频。')
    audio = np.concatenate(chunks).astype(np.float32) / 32768.0
    if len(audio) > MAX_SECONDS * 16000:
        raise ValueError('单条语音最多 10 分钟。')
    with _lock:
        if _model is None:
            if not (MODEL_DIR / 'model.bin').exists():
                raise RuntimeError('本地语音模型尚未安装完成。')
            _model = WhisperModel(str(MODEL_DIR), device='cpu', compute_type='int8', cpu_threads=6)
        segments, _ = _model.transcribe(audio, beam_size=5, vad_filter=True,
                                        condition_on_previous_text=False)
        return ''.join(segment.text for segment in segments).strip()
