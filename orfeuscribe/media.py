"""Long audio and video files, in bounded parts, through the same transcription and revision."""
from pathlib import Path
import time

from .i18n import _
from .pipeline import Cancelled, Result, run_pipeline
from .providers import ProviderError

EXTENSIONS = {'.mp4', '.mkv', '.wav', '.mp3', '.ogg', '.m4a', '.webm', '.flac'}
CHUNK_SECONDS = 300  # 9.6 MB of PCM: well under the 25 MB upload limit.


def timestamp(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600:02}:{seconds % 3600 // 60:02}:{seconds % 60:02}'


def audio_chunks(path, cancelled=lambda: False, chunk_seconds=CHUNK_SECONDS):
    """Decodes with backpressure: at most one part and eight buffers in memory."""
    import io
    import wave
    from .audio import Gst
    path = Path(path).resolve()
    if path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise ValueError(_('Choose an audio or video file (MP4, MKV, WebM, WAV, MP3, M4A, OGG or FLAC).'))
    if not 0 < chunk_seconds <= 600:
        raise ValueError('Each part must be at most ten minutes long.')
    pipeline = Gst.Pipeline.new('orfeuscribe-file')
    source = Gst.ElementFactory.make('uridecodebin', 'source')
    convert = Gst.ElementFactory.make('audioconvert', None)
    resample = Gst.ElementFactory.make('audioresample', None)
    caps = Gst.ElementFactory.make('capsfilter', None)
    sink = Gst.ElementFactory.make('appsink', 'chunks')
    if any(x is None for x in (source, convert, resample, caps, sink)):
        raise RuntimeError(_('A GStreamer component needed to open the file is missing.'))
    source.set_property('uri', path.as_uri())
    caps.set_property('caps', Gst.Caps.from_string('audio/x-raw,format=S16LE,rate=16000,channels=1,layout=interleaved'))
    sink.set_property('sync', False)
    sink.set_property('max-buffers', 8)
    sink.set_property('drop', False)
    for element in (source, convert, resample, caps, sink):
        pipeline.add(element)
    for left, right in ((convert, resample), (resample, caps), (caps, sink)):
        if not left.link(right):
            raise RuntimeError(_('Could not prepare the audio of the file.'))
    linked, decoded = [], []

    def pad_added(_source, pad):
        media_type = pad.query_caps(None).to_string()
        if media_type.startswith('audio/') and not linked:
            if pad.link(convert.get_static_pad('sink')) == Gst.PadLinkReturn.OK:
                linked.append(True)
        else:
            discard = Gst.ElementFactory.make('fakesink', None)
            discard.set_property('sync', False)
            discard.set_property('async', False)
            pipeline.add(discard)
            discard.sync_state_with_parent()
            pad.link(discard.get_static_pad('sink'))
    source.connect('pad-added', pad_added)
    source.connect('no-more-pads', lambda *_args: decoded.append(True))
    bus = pipeline.get_bus()
    pending, offset, duration = bytearray(), 0.0, 0.0
    limit = int(chunk_seconds * 32000) // 2 * 2

    def wav_bytes(pcm):
        output = io.BytesIO()
        with wave.open(output, 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        return output.getvalue()
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise ProviderError(_('Could not open the audio track.'))
        while True:
            if cancelled():
                raise Cancelled()
            sample = sink.emit('try-pull-sample', 100 * Gst.MSECOND)
            failure = bus.pop_filtered(Gst.MessageType.ERROR)
            if decoded and not linked:
                raise ProviderError(_('The file has no usable audio track.'))
            if failure:
                raise ProviderError(_('Could not decode the file. Check the format and the installed codecs.'))
            if sample:
                buffer = sample.get_buffer()
                pending.extend(buffer.extract_dup(0, buffer.get_size()))
                ok, nanos = pipeline.query_duration(Gst.Format.TIME)
                if ok and nanos > 0:
                    duration = nanos / Gst.SECOND
            while len(pending) >= limit:
                pcm = bytes(pending[:limit])
                del pending[:limit]
                yield wav_bytes(pcm), offset, len(pcm) / 32000, duration
                offset += len(pcm) / 32000
            if sink.get_property('eos'):
                break
        if pending:
            yield wav_bytes(bytes(pending)), offset, len(pending) / 32000, duration
        elif not linked:
            raise ProviderError(_('The file has no usable audio track.'))
    finally:
        pipeline.set_state(Gst.State.NULL)
        pending.clear()


def transcribe_file(provider, path, mode, vocab, *, input_language='en', history=None,
                    learn_from_history=False, understand_intent=True, cancelled=lambda: False,
                    progress=lambda *args: None, chunk_seconds=CHUNK_SECONDS):
    started = time.monotonic()
    raw_parts, final_parts, errors = [], [], []
    previous, stt, polishing = '', 0.0, 0.0
    context = {'wm_class': 'File', 'title': Path(path).name}
    chunks = audio_chunks(path, cancelled, chunk_seconds)
    try:
        for number, (audio, offset, seconds, duration) in enumerate(chunks, 1):
            if cancelled():
                raise Cancelled()
            context['previous_tail'] = previous

            def state(stage):
                progress(stage, number, offset, duration)
            result = run_pipeline(provider, audio, mode, context, vocab, state=state,
                                  cancelled=cancelled, history=history, learn_from_history=learn_from_history,
                                  understand_intent=understand_intent, input_language=input_language,
                                  allow_destinations=False, allow_voice_cancel=False)
            heading = f'## {timestamp(offset)} — ' + _('Part {number}').format(number=number) + '\n\n'
            raw_parts.append(heading + result.raw)
            final_parts.append(heading + (result.final or result.raw))
            previous = (result.final or result.raw)[-1200:]
            stt += result.stt
            polishing += result.polishing
            if result.error:
                errors.append(_('Part {number}: revision pending; the transcript was kept.').format(number=number))
            progress('part_done', number, offset + seconds, duration)
    finally:
        chunks.close()
    if cancelled():
        raise Cancelled()
    if not raw_parts:
        raise ProviderError(_('No audio was found to transcribe.'))
    return Result('\n\n'.join(raw_parts), '\n\n'.join(final_parts), mode, provider.name,
                  getattr(provider, 'last_model', provider.model), 0, stt, polishing,
                  time.monotonic() - started, error='\n'.join(errors), action='file', input_language=input_language)
