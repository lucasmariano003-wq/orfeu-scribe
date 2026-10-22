"""The pipeline and the guard against foreign text. Synthetic data only."""
import json
import unittest
from unittest.mock import patch

from orfeuscribe import guard
from orfeuscribe.pipeline import Cancelled, literal_local, run_pipeline
from orfeuscribe.providers import ProviderError, Service
from orfeuscribe.providers.openai_compat import OpenAIReviser
from orfeuscribe.text import LINE, PARAGRAPH, build_messages
from orfeuscribe.vocabulary import parse_vocabulary
from tests.support import FakeProvider

EMPTY = parse_vocabulary("")
EARLIER1 = ("Look in the shared notebooks for last year's talks about these readings, "
            "especially the one given on the first Sunday of spring.")
EARLIER2 = ("Then draft a new talk based on those notes and today's research, "
            "without quoting the weekly announcements or the bulletin.")
VOCAB = parse_vocabulary("Augustine\nChrysostom\nZephyrine Labs\nNimbus\nKestrel Park\nOrrery\n"
                         "Lumen Atlas\nnotebook lm => NotebookLM\n")
LONG = ("I need you to organize the week's calendar with the meetings, the visits and the board session, "
        "and tell me what conflicts.")


class GuardTests(unittest.TestCase):
    def test_loose_word_that_returns_with_earlier_messages(self):
        self.assertTrue(guard.check("Specials", f"Specials.\n\n{EARLIER1}\n\n{EARLIER2}", [EARLIER1, EARLIER2], VOCAB).leaked)

    def test_short_phrase_plus_another_dictation(self):
        verdict = guard.check("Ok, that is what I thought.", f"Ok, that is what I thought. {EARLIER2}", [EARLIER2], VOCAB)
        self.assertEqual(verdict, guard.Verdict(True, "history example"))

    def test_cut_speech_turned_into_a_vocabulary_list(self):
        verdict = guard.check("I want to pass to my brother all the...",
                              "I want to pass to my brother all the references: Augustine, Chrysostom, Zephyrine Labs, "
                              "Nimbus, Orrery and Lumen Atlas.", [], VOCAB)
        self.assertEqual(verdict, guard.Verdict(True, "vocabulary"))

    def test_short_test_with_invented_sentences(self):
        verdict = guard.check("This is just a test.", "This is just a test. I want to see if it works properly. "
                              "I think we need to change how the keyboard looks.", [], EMPTY)
        self.assertEqual(verdict, guard.Verdict(True, "short speech inflated"))

    def test_legitimate_long_reorganization_passes(self):
        raw = ("I had an idea, uh, for the language bot. Every day I listen to the news, right, so it could have a "
               "routine that plays the news in Portuguese and right after, in the same audio, the reading in English, "
               "slow and easy, so I get used to it, because I know the stories well.")
        revised = ("Create a language study bot based on the daily news.\n\nEvery day, a single audio with the news in "
                   "Portuguese followed by the same reading in English, slow and easy. I know the stories well, and "
                   "that helps me get used to the language.")
        self.assertFalse(guard.check(raw, revised, [EARLIER1, EARLIER2], VOCAB).leaked)

    def test_fixing_names_from_the_glossary_passes(self):
        raw = "Lemember the zephyrin labs, the chrysostom and the augustin in today's research, I want all three."
        revised = "Remember Zephyrine Labs, Chrysostom and Augustine in today's research: I want all three."
        self.assertFalse(guard.check(raw, revised, [], VOCAB).leaked)

    def test_heard_vocabulary_limits_the_second_attempt(self):
        heard = guard.vocabulary_heard("talk about augustine and notebook lm", VOCAB)
        self.assertEqual(heard.terms, ("Augustine", "NotebookLM"))
        self.assertEqual(heard.corrections, (("notebook lm", "NotebookLM"),))
        self.assertEqual(guard.words("Café, ÉPOCA—já!"), ["cafe", "epoca", "ja"])
        self.assertTrue(guard.is_short("eleven " * 11))
        self.assertFalse(guard.is_short("twelve " * 12))


class History:
    def __init__(self, *examples):
        self.examples = examples

    def style_examples(self, mode, language=None, exclude_id=None):
        return self.examples


def run(provider, raw, **kwargs):
    kwargs.setdefault("history", History(EARLIER2))
    kwargs.setdefault("learn_from_history", True)
    return run_pipeline(provider, b"", kwargs.pop("mode", "dictation"), kwargs.pop("context", {}),
                        kwargs.pop("vocab", EMPTY), raw=raw, understand_intent=False, **kwargs)


class FlowTests(unittest.TestCase):
    def test_commands_line_breaks_and_vocabulary_after_the_model(self):
        for mode in ("dictation", "message", "email", "prompt", "literal"):
            for rendered in (False, True):
                with self.subTest(mode=mode, rendered=rendered):
                    reply = ("First.\n“at three”.\n\nzefirin returns." if rendered else
                             f"First. {LINE} “at three”. {PARAGRAPH} zefirin returns.")
                    provider = FakeProvider(replies=[reply])
                    result = run_pipeline(provider, b"", mode, {}, parse_vocabulary("zefirin => Zephyrine"),
                                          raw="First. New line, “at three”. New paragraph, zefirin returns. New line.")
                    self.assertEqual(result.final, "First.\n“At three”.\n\nZephyrine returns.\n")
                    self.assertFalse(result.error)

    def test_states_and_times(self):
        states = []
        provider = FakeProvider(raw="hello new paragraph", replies=["Hello"])
        result = run_pipeline(provider, b"audio", "dictation", {}, EMPTY, recording=2, state=states.append)
        self.assertEqual(result.final, "Hello\n\n")
        self.assertEqual(states, ["transcribing", "polishing"])
        self.assertGreaterEqual(result.total, 2)

    def test_cancellation(self):
        for kwargs in ({"cancelled": lambda: True}, {"raw": "cancel dictation"}):
            with self.assertRaises(Cancelled):
                run_pipeline(FakeProvider(), b"audio", "dictation", {}, EMPTY, **kwargs)
        result = run_pipeline(FakeProvider(), b"", "dictation", {}, EMPTY, raw="Cancel dictation.",
                              allow_destinations=False, allow_voice_cancel=False)
        self.assertEqual(result.final, "Cancel dictation.")

    def test_revision_failure_keeps_the_transcript(self):
        result = run_pipeline(FakeProvider(raw="hello new paragraph", fail_polish=True), b"audio", "dictation", {}, EMPTY)
        self.assertEqual((result.raw, result.final), ("hello new paragraph", ""))
        self.assertIn("transcript was kept", result.error)

    def test_missing_line_command_refuses_delivery(self):
        result = run_pipeline(FakeProvider(replies=["First. Second."]), b"", "dictation", {}, EMPTY,
                              raw="First. New line, Second.")
        self.assertEqual(result.final, "")
        self.assertTrue(result.error)

    def test_cancel_during_provider_work_drops_the_result(self):
        provider = FakeProvider()
        flag = []
        original = provider.polish
        provider.polish = lambda *args, **kwargs: (flag.append(True), original(*args, **kwargs))[1]
        with self.assertRaises(Cancelled):
            run_pipeline(provider, b"audio", "dictation", {}, EMPTY, cancelled=lambda: bool(flag))

    def test_short_dictation_goes_without_examples(self):
        provider = FakeProvider(replies=["Specials."])
        result = run(provider, "Specials")
        self.assertEqual((result.final, result.model), ("Specials.", "fake-model"))
        self.assertEqual(provider.calls[0]["examples"], ())

    def test_examples_only_when_learning_is_on(self):
        provider = FakeProvider(replies=[LONG])
        run(provider, LONG)
        self.assertEqual(provider.calls[0]["examples"], (EARLIER2,))
        provider = FakeProvider(replies=[LONG])
        run(provider, LONG, learn_from_history=False)
        self.assertEqual(provider.calls[0]["examples"], ())

    def test_leak_is_redone_without_support_and_with_heard_vocabulary(self):
        provider = FakeProvider(replies=[f"{LONG} {EARLIER2}", LONG])
        result = run(provider, LONG + " Mention augustine too.", vocab=VOCAB)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[1]["examples"], ())
        self.assertEqual(provider.calls[1]["vocab"].terms, ("Augustine",))
        self.assertEqual(result.final, LONG)
        self.assertEqual(result.model, "fake-model (guard: history example leaked; redone without support)")

    def test_repeated_leak_delivers_what_was_said(self):
        leaked = f"{LONG}{LINE}Thanks. {EARLIER2}"
        provider = FakeProvider(replies=[leaked, leaked])
        result = run(provider, LONG[0].lower() + LONG[1:] + " New line. thanks.")
        self.assertEqual(result.final, LONG + "\nThanks.")
        self.assertTrue(result.model.startswith("local cleanup (guard: history example"), result.model)
        self.assertFalse(result.error)

    def test_failing_second_attempt_also_delivers_what_was_said(self):
        provider = FakeProvider(replies=[f"{LONG} {EARLIER2}", ProviderError("HTTP 500")])
        result = run(provider, LONG)
        self.assertEqual(result.final, LONG)
        self.assertTrue(result.model.startswith("local cleanup"))

    def test_previous_part_is_watched_support(self):
        earlier = ("and so we finish the first chapter of the class about the history of the printing press "
                   "in the fifteenth century")
        provider = FakeProvider(replies=[f"{earlier}. Now the second point of today's class about paper.",
                                         "Now the second point of today's class about paper."])
        result = run(provider, "now the second point of today's class about paper",
                     context={"previous_tail": earlier}, history=None)
        self.assertEqual(result.final, "Now the second point of today's class about paper.")
        self.assertNotIn("previous_tail", provider.calls[1]["context"])

    def test_literal_and_translation_skip_the_guard(self):
        provider = FakeProvider(replies=["This is a test and many more words nobody said here today at all."])
        result = run(provider, "this is a test", mode="literal")
        self.assertEqual(len(provider.calls), 1)
        provider = FakeProvider(replies=["Esto es una prueba con muchas palabras que nadie dijo hoy aquí."])
        result = run(provider, "this is a test", operation="translation", language="Spanish")
        self.assertEqual(result.mode, "translation")
        self.assertEqual(result.language, "Spanish")

    def test_language_prefix_transcribes_again_with_the_language(self):
        provider = FakeProvider(raw="In Latin: Pax vobiscum.")
        result = run_pipeline(provider, b"wav", "dictation", {}, EMPTY, input_language="en")
        self.assertEqual(provider.languages, ["en", "la"])
        self.assertEqual(result.input_language, "la")
        prompt = build_messages("Pax vobiscum", "dictation", {"input_language": "la"}, EMPTY)
        self.assertIn("INPUT LANGUAGE: Latin", prompt[0]["content"])

    def test_local_cleanup_without_a_revision_service(self):
        provider = FakeProvider(raw="hi zefirin, full stop. see you tomorrow, new line, bye", can_revise=False)
        result = run_pipeline(provider, b"wav", "dictation", {}, parse_vocabulary("zefirin => Zephyrine"))
        self.assertEqual(result.final, "Hi Zephyrine. See you tomorrow\nBye")
        self.assertEqual(provider.calls, [], "no model was called")
        self.assertIn("local cleanup", result.model)
        self.assertFalse(result.error)
        service = Service(FakeProvider(raw="hola"), None)
        result = run_pipeline(service, b"wav", "dictation", {}, EMPTY, operation="translation", language="German")
        self.assertIn("text revision service", result.error)

    def test_literal_local(self):
        vocab = parse_vocabulary("zefirin => Zephyrine")
        self.assertEqual(literal_local("in latin: pater noster. New line. qui es in caelis", "dictation", vocab, ("la", "en")),
                         "Pater noster.\nQui es in caelis")
        self.assertEqual(literal_local("uh, I read zefirin, full stop. later", "dictation", vocab), "Uh, I read Zephyrine. Later")
        self.assertEqual(literal_local("new point grace. quotation the just live by faith", "outline", vocab),
                         "Point 1\nGrace.\n\nQuotation\n> The just live by faith")

    def test_notes_route_only_when_configured(self):
        result = run(FakeProvider(), "Note in Ideas: buy a lamp.", notes=True, history=None)
        self.assertEqual((result.action, result.note_folder, result.mode), ("note", "Ideas", "note"))
        result = run(FakeProvider(), "Note in Ideas: buy a lamp.", history=None)
        self.assertEqual(result.action, "deliver")

    def test_structure_missing_refuses_delivery(self):
        result = run_pipeline(FakeProvider(replies=["Text without point."]), b"", "outline", {}, EMPTY,
                              raw="New point: listen.")
        self.assertTrue(result.error)
        self.assertFalse(result.final)

    def test_intent_revision_never_gets_examples_for_short_speech(self):
        sent = []
        reviser = OpenAIReviser("https://example.invalid/v1", "model")

        def respond(path, data):
            sent.append(json.loads(data))
            return {"choices": [{"message": {"content": "Specials."}}]}
        with patch.object(reviser.client, "request", respond):
            service = Service(FakeProvider(), reviser)
            run_pipeline(service, b"", "dictation", {}, EMPTY, raw="Specials", history=History(EARLIER2),
                         learn_from_history=True, understand_intent=True)
            run_pipeline(service, b"", "dictation", {}, EMPTY, raw=LONG, history=History(EARLIER2),
                         learn_from_history=True, understand_intent=True)
        self.assertNotIn("style_examples", sent[0]["messages"][1]["content"])
        self.assertIn("style_examples", sent[1]["messages"][1]["content"])
        self.assertIn("ONLY TASK: EDIT A DICTATION", sent[0]["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
