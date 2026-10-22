"""Launcher entry point and command-line tools that work without the interface."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .i18n import _, ngettext


def _service(config, observer=None):
    from . import providers
    return providers.build(config, prompt_observer=observer)


def test_audio(args):
    """Transcribes and revises a WAV with the configured service; nothing is recorded or pasted."""
    from .config import Config, DATA_DIR
    from .history import FILENAME, History
    from .pipeline import Cancelled, run_pipeline
    from .providers import ProviderError
    from .vocabulary import FILENAME as VOCABULARY, load_vocabulary
    import wave
    try:
        path = Path(args.test_audio)
        with wave.open(str(path), "rb") as wav:
            seconds = wav.getnframes() / wav.getframerate()
        if path.stat().st_size > 25 * 1024 * 1024:
            print(_("Error: the file is larger than 25 MB."), file=sys.stderr)
            return 1
        config = Config()
        prompts = []
        provider = _service(config, prompts.append if args.show_prompt else None)
        history = History(Path(args.history)) if args.history else History(DATA_DIR / FILENAME)
        language = args.language or config["input_language"]
        result = run_pipeline(provider, path.read_bytes(), args.mode,
            {"wm_class": "orfeuscribe-cli", "title": path.name}, load_vocabulary(config.directory / VOCABULARY),
            history=history, learn_from_history=config["learn_from_history"],
            understand_intent=config["understand_intent"], operation=args.operation,
            language=args.target_language, input_language=language, default_language=config["input_language"],
            notes=False)
        output = result.as_dict()
        output["audio_seconds"] = seconds
        if args.save:
            output["history_id"] = history.add(app="orfeuscribe-cli", title=path.name, **result.as_dict())
        if args.show_prompt:
            output["prompt"] = prompts[0] if prompts else []
        if args.json:
            print(json.dumps(output, ensure_ascii=False, indent=2))
        else:
            print(f"Service: {result.provider}\nModel: {result.model}\nAudio: {seconds:.2f}s")
            print(f"\nTRANSCRIPT\n{result.raw}\n\nFINAL\n{result.final}")
            print(f"\nTIMINGS\nTranscription: {result.stt:.3f}s\nRevision: {result.polishing:.3f}s\nTotal: {result.total:.3f}s")
            if result.error:
                print(f"Error: {result.error}")
            if args.show_prompt:
                print("\nPROMPT\n" + json.dumps(output["prompt"], ensure_ascii=False, indent=2))
        return 1 if result.error else 0
    except Cancelled:
        print(_("Dictation cancelled by the spoken command. No text delivered."))
        return 0
    except ProviderError as exc:
        print(_("Error: {message}").format(message=exc), file=sys.stderr)
        return 1
    except (OSError, wave.Error, EOFError):
        print(_("Error: could not read the WAV file."), file=sys.stderr)
        return 1


def history_command(args):
    from .config import Config, DATA_DIR
    from .exchange import export_history, import_history
    from .history import FILENAME, History
    from .pipeline import Cancelled
    from .providers import ProviderError
    import sqlite3
    try:
        history = History(Path(args.history) if args.history else DATA_DIR / FILENAME)
        if args.export:
            count = export_history(history, args.export, args.search)
            print(ngettext("{count} entry exported with private permissions.",
                           "{count} entries exported with private permissions.", count).format(count=count))
        elif args.import_file:
            count = import_history(history, args.import_file)
            print(ngettext("{count} entry added to the history.", "{count} entries added to the history.",
                           count).format(count=count))
        else:
            from .media import transcribe_file
            from .vocabulary import FILENAME as VOCABULARY, load_vocabulary
            config = Config()
            result = transcribe_file(_service(config), args.transcribe, args.mode,
                load_vocabulary(config.directory / VOCABULARY), input_language=args.language or config["input_language"],
                history=history, learn_from_history=config["learn_from_history"],
                understand_intent=config["understand_intent"],
                progress=lambda stage, number, position, duration: print(f"Part {number}: {stage}", file=sys.stderr))
            ident = history.add(app='File', title=Path(args.transcribe).name, **result.as_dict())
            print(json.dumps(dict(id=ident, **result.as_dict()), ensure_ascii=False, indent=2) if args.json
                  else _("Transcription saved as history entry {ident}.").format(ident=ident))
            return 1 if result.error else 0
        return 0
    except (KeyboardInterrupt, Cancelled):
        print(_("Transcription cancelled. Nothing was added."), file=sys.stderr)
        return 130
    except ProviderError as exc:
        print(_("Error: {message}").format(message=exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        print(_("Could not finish. Check the file, the permissions and the connection."), file=sys.stderr)
        return 1


def main():
    os.umask(0o077)
    from .languages import LANGUAGES
    from .modes import MODES
    parser = argparse.ArgumentParser(prog="orfeuscribe", description="Orfeu Scribe — voice dictation for GNOME")
    parser.add_argument("--version", action="version", version=f"Orfeu Scribe {__version__}")
    parser.add_argument("--background", action="store_true", help="start the service without opening the window")
    parser.add_argument("--test-audio", metavar="FILE.wav",
                        help="transcribe and revise a WAV with the configured service, without recording or pasting")
    parser.add_argument("--mode", choices=list(MODES), default="dictation")
    parser.add_argument("--language", choices=list(LANGUAGES), help="spoken language (default: the one in Settings)")
    parser.add_argument("--operation", choices=["dictation", "translation", "ask"], default="dictation")
    parser.add_argument("--target-language", default="English", help="target language for --operation translation")
    parser.add_argument("--transcribe", metavar="FILE", help="transcribe an audio or video file into the history")
    parser.add_argument("--export", metavar="HISTORY.md|HISTORY.csv", help="export the history to a private file")
    parser.add_argument("--import", dest="import_file", metavar="HISTORY.md|HISTORY.csv",
                        help="add entries from a Orfeu Scribe export")
    parser.add_argument("--search", default="", help="export only entries containing these words")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--show-prompt", action="store_true",
                        help="with --test-audio, print the messages sent for revision (never keys)")
    parser.add_argument("--history", metavar="FILE.db", help="use a separate history database")
    parser.add_argument("--save", action="store_true", help="with --test-audio, save the result in the history")
    args = parser.parse_args()
    if args.test_audio:
        return test_audio(args)
    if args.export or args.import_file or (args.transcribe and args.history):
        return history_command(args)
    try:
        from .service import OrfeuScribeApplication
        app = OrfeuScribeApplication(background=args.background)
        arguments = [sys.argv[0], *(["--background"] if args.background else [])]
        if args.transcribe:
            arguments.extend(['--transcribe', str(Path(args.transcribe).resolve()), '--mode', args.mode])
            if args.language:
                arguments.extend(['--language', args.language])
        return app.run(arguments)
    except (ImportError, ValueError):
        print(_("Orfeu Scribe needs the system Python with GTK 4, libadwaita and GStreamer. "
                "Start it from the installed launcher."), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
