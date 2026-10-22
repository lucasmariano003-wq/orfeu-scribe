"""OpenAI-compatible adapters against a local fake server. No real service is called."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orfeuscribe import providers
from orfeuscribe.config import Config, valid_endpoint
from orfeuscribe.credentials import ENVIRONMENT, MemoryStore, api_key
from orfeuscribe.providers import ProviderError, Unconfigured
from orfeuscribe.providers.openai_compat import HttpClient, OpenAIReviser, OpenAITranscriber, clean_terms, hint_style
from orfeuscribe.vocabulary import parse_vocabulary
from tests.fake_openai import SECRET_ECHO, serve
from tests.support import wav

KEY = "sk-test-not-a-real-key-123"


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        self.server = serve()
        self.addCleanup(self.server.close)
        self.state = self.server.state

    def fields(self, index=-1):
        return self.state.transcriptions[index]


class TranscriberTests(FakeServerCase):
    def test_local_endpoint_bypasses_environment_proxy(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1",
                                     "HTTP_PROXY": "http://127.0.0.1:1",
                                     "no_proxy": "", "NO_PROXY": ""}):
            client = OpenAITranscriber(self.server.url, "local", key=lambda: KEY)
            self.assertEqual(client.transcribe(wav(1)), self.state.text)
        self.assertEqual(self.state.authorizations, ["Bearer " + KEY])

    def test_gpt_transcribe_uses_languages_and_keywords(self):
        stt = OpenAITranscriber(self.server.url, "gpt-transcribe", key=lambda: KEY)
        self.assertEqual(stt.transcribe(wav(1), "pt", ("Zephyrine", "AC-42", "bad<term>", "x" * 60)),
                         self.state.text)
        fields = dict((name, value) for name, value in self.fields() if name != "keywords[]")
        self.assertEqual(fields["model"], "gpt-transcribe")
        self.assertEqual(fields["languages[]"], "pt")
        self.assertNotIn("language", fields, "never both language fields")
        self.assertEqual([v for n, v in self.fields() if n == "keywords[]"], ["Zephyrine", "AC-42"])
        self.assertEqual(fields["file"]["filename"], "dictation.wav")
        self.assertTrue(fields["file"]["riff"])
        self.assertEqual(self.fields()[-1][0], "file", "the file goes last")
        self.assertEqual(self.state.authorizations[-1], "Bearer " + KEY)

    def test_other_models_use_language_and_prompt(self):
        stt = OpenAITranscriber(self.server.url, "whisper-1", key=lambda: KEY)
        stt.transcribe(wav(1), "es", [f"term{n}" for n in range(200)])
        fields = dict(self.fields())
        self.assertEqual(fields["language"], "es")
        self.assertNotIn("languages[]", fields)
        self.assertTrue(fields["prompt"].startswith("Vocabulary: term0, term1"))
        self.assertLessEqual(len(fields["prompt"]), 600)
        self.assertTrue(fields["prompt"].endswith("."))

    def test_explicit_hint_styles(self):
        for hints, present, absent in (("keywords", "languages[]", "language"), ("prompt", "language", "languages[]"),
                                       ("none", "model", "language")):
            with self.subTest(hints=hints):
                OpenAITranscriber(self.server.url, "any", key=lambda: KEY, hints=hints).transcribe(wav(1), "en", ("Term",))
                names = [name for name, _ in self.fields()]
                self.assertIn(present, names)
                self.assertNotIn(absent, names)
        self.assertEqual(hint_style("auto", "GPT-Transcribe-2"), "keywords")
        self.assertEqual(clean_terms(["a\nb", "  c  ", "c"]), ["a b", "c"])

    def test_keyless_local_server_sends_no_authorization(self):
        OpenAITranscriber(self.server.url, "local", key=lambda: None).transcribe(wav(1), "en")
        self.assertIsNone(self.state.authorizations[-1])

    def test_errors_are_mapped_and_never_echo_the_body(self):
        stt = OpenAITranscriber(self.server.url, "model", key=lambda: KEY)
        for status, fragment in ((401, "API key"), (404, "Endpoint or model"), (413, "too large"),
                                 (429, "Rate limit"), (500, "HTTP 500"), (503, "HTTP 503")):
            with self.subTest(status=status):
                self.state.fail["transcriptions"] = [status]
                with self.assertRaises(ProviderError) as caught:
                    stt.transcribe(wav(1), "en")
                self.assertIn(fragment, str(caught.exception))
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn(SECRET_ECHO, str(caught.exception))
                self.assertNotIn(KEY, str(caught.exception))

    def test_empty_text_and_empty_audio(self):
        self.state.text = "   "
        stt = OpenAITranscriber(self.server.url, "model")
        with self.assertRaises(ProviderError):
            stt.transcribe(wav(1), "en")
        with self.assertRaises(ProviderError):
            stt.transcribe(b"", "en")

    def test_unreachable_server(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        stt = OpenAITranscriber(f"http://127.0.0.1:{port}/v1", "model", key=lambda: KEY, timeout=2)
        with self.assertRaises(ProviderError) as caught:
            stt.transcribe(wav(1), "en")
        self.assertIn("Could not reach", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_redirects_are_refused(self):
        client = HttpClient(self.server.url + "/redirect", key=lambda: KEY)
        with self.assertRaises(ProviderError) as caught:
            client.list_models()
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(len(self.state.authorizations), 1, "the key was not carried to the redirect target")

    def test_list_models(self):
        self.assertEqual(OpenAITranscriber(self.server.url, "m").list_models(), ["fake-chat", "fake-transcribe"])


class ReviserTests(FakeServerCase):
    def reviser(self, **kwargs):
        return OpenAIReviser(self.server.url, "fake-chat", key=lambda: KEY, **kwargs)

    def test_payload_is_minimal_and_messages_are_observed(self):
        seen = []
        result = self.reviser(prompt_observer=seen.append).polish("hello there", "dictation",
                                                                 {"input_language": "en"}, parse_vocabulary(""))
        self.assertEqual(result, "Hello there.")
        request = self.state.chats[-1]
        self.assertEqual(set(request), {"model", "messages"}, "no max_tokens, temperature or extras")
        self.assertEqual(seen[0], request["messages"])
        self.assertNotIn(KEY, json.dumps(seen))

    def test_literal_fidelity_does_not_depend_on_the_model(self):
        self.state.reply = "A completely different sentence."
        self.assertEqual(self.reviser().polish("so uh yes", "literal", {}, parse_vocabulary("")), "so uh yes")

    def test_length_limit_and_empty_answers(self):
        reviser = self.reviser()
        self.state.reply = ""
        with self.assertRaises(ProviderError):
            reviser.polish("text", "dictation", {}, parse_vocabulary(""))

    def test_structured_assist_falls_back_when_unsupported(self):
        reviser = self.reviser()
        self.state.reject_schema = True
        reply = reviser.assist("summarize", "A text.", {}, parse_vocabulary(""))
        self.assertEqual(reply["action"], "question")
        self.assertIn("response_format", self.state.chats[0])
        self.assertNotIn("response_format", self.state.chats[1])
        reviser.assist("summarize", "A text.", {}, parse_vocabulary(""))
        self.assertNotIn("response_format", self.state.chats[2], "remembered for the session")

    def test_assist_rejects_unsafe_answers(self):
        self.state.reply = "not json"
        with self.assertRaises(ProviderError):
            self.reviser().assist("x", "y", {}, parse_vocabulary(""))

    def test_translate_write_ask(self):
        reviser = self.reviser()
        self.state.reply = "**Hola** a todos"
        self.assertEqual(reviser.write("a greeting", {}, parse_vocabulary("")), "Hola a todos")
        self.assertEqual(reviser.ask("why?", {}, parse_vocabulary("")), ("**Hola** a todos", ""))
        reviser.translate("hello", "dictation", {"input_language": "en"}, parse_vocabulary(""), "Spanish")
        self.assertIn("write the result in Spanish", self.state.chats[-1]["messages"][0]["content"])


class EndpointTests(unittest.TestCase):
    def test_only_https_or_local_http(self):
        for url, ok in (("https://api.openai.com/v1", True), ("http://127.0.0.1:8080/v1", True),
                        ("http://localhost:9000/v1", True), ("http://[::1]:9000/v1", True),
                        ("http://example.com/v1", False), ("https://user:pw@example.com/v1", False),
                        ("https://example.com/v1?x=1", False), ("ftp://example.com", False), ("", False)):
            with self.subTest(url=url):
                self.assertEqual(valid_endpoint(url), ok)
        with self.assertRaises(ProviderError):
            HttpClient("http://example.com/v1")


class BuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Config(Path(temp.name))

    def test_unconfigured_service_sends_nothing(self):
        service = providers.build(self.config, MemoryStore())
        self.assertIsInstance(service.transcriber, Unconfigured)
        self.assertFalse(service.configured)
        with self.assertRaises(ProviderError) as caught:
            service.transcribe(b"RIFF", "en")
        self.assertIn("Settings", str(caught.exception))

    def test_revision_reuses_or_separates_server_and_key(self):
        self.config.update(stt_base_url="https://stt.example/v1", stt_model="gpt-transcribe")
        service = providers.build(self.config, MemoryStore())
        self.assertIsNone(service.reviser, "no revision model yet")
        self.config.update(revision_model="chat-model")
        store = MemoryStore({"stt": "stt-key", "revision": "revision-key"})
        service = providers.build(self.config, store)
        self.assertEqual(service.reviser.client.base, "https://stt.example/v1")
        self.assertEqual(service.reviser.client._key(), "stt-key")
        self.config.update(revision_same_server=False, revision_base_url="https://llm.example/v1")
        service = providers.build(self.config, store)
        self.assertEqual((service.reviser.client.base, service.reviser.client._key()),
                         ("https://llm.example/v1", "revision-key"))
        self.config.update(revision_enabled=False)
        self.assertFalse(providers.build(self.config, store).can_revise)

    def test_environment_key_wins_and_is_read_at_request_time(self):
        store = MemoryStore({"stt": "from-store"})
        self.assertEqual(api_key(store, "stt"), "from-store")
        with patch.dict(os.environ, {ENVIRONMENT["stt"]: " from-env "}):
            self.assertEqual(api_key(store, "stt"), "from-env")
        self.assertIsNone(api_key(MemoryStore(), "revision"))
        with self.assertRaises(ValueError):
            api_key(store, "other")

    def test_keys_never_reach_the_settings_file(self):
        self.config.update(stt_base_url="https://stt.example/v1", stt_model="m")
        stored = json.loads(self.config.path.read_text())
        self.assertFalse({key for key in stored if "key" in key.lower() and key != "stt_hints"})
        self.config.path.write_text(json.dumps({"api_key": "sk-leak", "stt_model": "m", "bearer": "x"}))
        loaded = Config(self.config.directory)
        loaded.save()
        self.assertNotIn("sk-leak", loaded.path.read_text())
        self.assertEqual(loaded.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
