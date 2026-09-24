"""Which voice says its lines, and what it is told about it.

Nothing here starts a model, a call or a speak server. The two roads out of
this room are a setting and a probe, and every answer below is decided before
any audio exists -- which is the part that matters, because the same reading
decides both what he sees on the bar and what it is told in its prompt. Those
two cannot be allowed to disagree.

    python -m server.test_voice_backend
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import brain

# What Breeze answers when asked which sounds the next line could make.
SOUNDS = ("laugh", "sigh", "cough", "clears throat")


class VoiceFile(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.path = self.temp / "voice.json"
        self.path.write_text(json.dumps({"enabled": True, "port": 8765, "voice": "assistant"}),
                             encoding="utf-8")
        patcher = patch.object(brain, "VOICE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        brain._VOICE_SEEN.update(at=0.0, was=None)
        self.addCleanup(brain._VOICE_SEEN.update, at=0.0, was=None)


class ChoosingTheRoad(VoiceFile):
    def test_her_own_voice_is_the_answer_when_nobody_has_chosen(self):
        self.assertEqual(brain.voice_backend(), "local")

    def test_a_word_that_is_not_a_road_is_not_followed(self):
        brain.write_voice(backend="kyutai")
        self.assertEqual(brain.voice_backend(), "local")

    def test_writing_one_setting_keeps_the_others(self):
        brain.write_voice(backend="openai")
        cfg = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["backend"], "openai")
        self.assertEqual(cfg["port"], 8765)
        self.assertEqual(cfg["voice"], "assistant")

    def test_a_write_drops_the_cached_reading(self):
        brain._VOICE_SEEN.update(at=9e9, was={"on": True})
        brain.write_voice(enabled=False)
        self.assertIsNone(brain._VOICE_SEEN["was"])


class WhatSheIsTold(VoiceFile):
    def status(self, served=None, active=False, **cfg):
        if cfg:
            brain.write_voice(**cfg)
        native = {"active": active, "voice": "sol"}
        with patch.object(brain, "live_voice", create=True), \
             patch("server.live_voice.manager") as manager:
            manager.status.return_value = native
            if served is None:
                with patch("urllib.request.urlopen", side_effect=OSError("refused")):
                    return brain.voice_status(fresh=True)
            with patch("urllib.request.urlopen",
                       return_value=io.BytesIO(json.dumps(served).encode())):
                return brain.voice_status(fresh=True)

    def ready(self, **changes):
        return {"enabled": True, "ready": True, "voice": "assistant",
                "engine": "qwen", "instruction": True, "speaking": False, **changes}

    def test_a_live_call_owns_the_audio_and_says_so(self):
        said = self.status(active=True)
        self.assertFalse(said["on"])
        self.assertEqual(said["backend"], "openai")

    def test_choosing_openai_without_a_call_is_silence_not_the_speak_server(self):
        # The old behaviour spoke through the app whenever no call was running.
        # With a road actually chosen, that would be the road nobody picked.
        with patch("urllib.request.urlopen", side_effect=AssertionError("probed anyway")):
            said = self.status(backend="openai")
        self.assertFalse(said["on"])
        self.assertEqual(said["backend"], "openai")

    def test_her_own_voice_warm_and_on_is_the_only_yes(self):
        said = self.status(self.ready())
        self.assertTrue(said["on"])
        self.assertEqual(said["backend"], "local")
        self.assertEqual(said["engine"], "qwen")
        self.assertTrue(said["sound"])

    def test_an_engine_without_moods_is_not_offered_as_one(self):
        said = self.status(self.ready(engine="pocket", instruction=False))
        self.assertTrue(said["on"])
        self.assertFalse(said["sound"])

    def test_an_engine_that_laughs_says_which_sounds_it_makes(self):
        said = self.status(self.ready(engine="breeze", events=list(SOUNDS)))
        self.assertTrue(said["sound"])
        self.assertEqual(said["events"], list(SOUNDS))

    def test_a_server_from_before_sounds_offers_none(self):
        # Qwen's answer has no list at all, and a list that is not one is no
        # list either -- never a laugh made of the letters of "laugh".
        self.assertEqual(self.status(self.ready())["events"], [])
        self.assertEqual(self.status(self.ready(events="laugh"))["events"], [])
        self.assertEqual(self.status(self.ready(events=["laugh", 3, " "]))["events"],
                         ["laugh"])

    def test_every_refusal_still_names_the_road_it_refused_on(self):
        for served, cfg in ((None, {}), (self.ready(enabled=False), {}),
                            (self.ready(ready=False), {}), (None, {"enabled": False})):
            said = self.status(served, **cfg)
            self.assertFalse(said["on"])
            self.assertEqual(said["backend"], "local")
            self.assertTrue(said["reason"])


class TheField(unittest.TestCase):
    def test_the_field_appears_only_where_it_would_be_performed(self):
        for voice_on, sound, present in ((True, True, True), (True, False, False),
                                         (False, True, False), (False, False, False)):
            schema = brain.response_schema(voice_on, sound)
            with self.subTest(voice_on=voice_on, sound=sound):
                self.assertEqual("sound" in schema["properties"], present)
                self.assertEqual("sound" in schema["required"], present)

    def test_the_prompt_describes_it_when_the_schema_offers_it(self):
        # A field it is given without a word about it is a field it fills in
        # blind; a section about a field it does not have is a promise broken.
        self.assertIn("`sound`", brain.harness_text(True, True))
        self.assertNotIn("`sound`", brain.harness_text(True, False))
        self.assertNotIn("{{", brain.harness_text(True, True))
        self.assertNotIn("{{", brain.harness_text(False, False))

    def test_the_operation_manual_never_leaves_a_placeholder_behind(self):
        for voice_on, sound in ((True, True), (True, False), (False, False)):
            self.assertNotIn("{{sound_section}}", brain.operation_harness(voice_on, sound))
            self.assertNotIn("{{voice_field}}", brain.operation_harness(voice_on, sound))
        for voice_on, sound in ((True, True), (True, False), (False, True)):
            for text in (brain.operation_harness(voice_on, sound, SOUNDS),
                         brain.harness_text(voice_on, sound, SOUNDS)):
                self.assertNotIn("{{", text)

    def test_the_sounds_are_described_only_while_the_engine_makes_them(self):
        # Offered on the speak server's word and on nothing else: no list, no
        # section; voice off, no section, whatever the list says.
        told = brain.harness_text(True, True, SOUNDS)
        self.assertIn("Sounds, written where they happen", told)
        self.assertIn("(laugh) (sigh) (cough) (clears throat)", told)
        self.assertNotIn("Sounds, written where they happen", brain.harness_text(True, True))
        self.assertNotIn("Sounds, written where they happen",
                         brain.harness_text(False, False, SOUNDS))

    def test_sounds_and_a_mood_are_offered_apart(self):
        # An engine may have either without the other. Sounds alone never
        # mention a field the schema does not have.
        only_sounds = brain.sound_sections(True, False, SOUNDS)
        self.assertIn("(sigh)", only_sounds)
        self.assertNotIn("`sound`", only_sounds)
        only_mood = brain.sound_sections(True, True, ())
        self.assertIn("`sound`", only_mood)
        self.assertNotIn("(laugh)", only_mood)
        self.assertEqual(brain.sound_sections(False, True, SOUNDS), "")

    def test_the_laugh_example_comes_only_with_a_laugh(self):
        self.assertIn("ha ha", brain.sound_sections(True, False, SOUNDS))
        sighs = brain.sound_sections(True, False, ("sigh",))
        self.assertIn("(sigh)", sighs)
        self.assertNotIn("ha ha", sighs)
        self.assertNotIn("(laugh)", sighs)


class SendingItOn(VoiceFile):
    def spoke(self, answered=None, **kwargs):
        sent = {}

        def fake(req, timeout=None):
            sent["url"] = req.full_url
            sent["body"] = json.loads(req.data)
            return io.BytesIO(json.dumps(answered or {}).encode())

        with patch("urllib.request.urlopen", side_effect=fake):
            return brain.say_aloud("Hello", **kwargs), sent

    def test_the_mood_rides_beside_the_words(self):
        heard, sent = self.spoke({"instructed": True}, sound="  sound daring and brave ")
        self.assertEqual(sent["body"]["text"], "Hello")
        self.assertEqual(sent["body"]["instruction"], "sound daring and brave")
        self.assertEqual(heard["sound"], "sound daring and brave")
        self.assertTrue(heard["performed"])

    def test_a_server_that_ignored_it_is_not_reported_as_having_used_it(self):
        heard, _ = self.spoke({"instructed": False}, sound="sound tired")
        self.assertTrue(heard["spoken"])
        self.assertFalse(heard["performed"])

    def test_no_mood_sends_an_empty_one_and_claims_nothing(self):
        heard, sent = self.spoke({})
        self.assertEqual(sent["body"]["instruction"], "")
        self.assertNotIn("sound", heard)
        self.assertNotIn("performed", heard)
        self.assertNotIn("events", heard)
        self.assertNotIn("events_dropped", heard)

    def test_the_sounds_are_reported_as_the_server_counted_them(self):
        # The laugh rides inside the words, untouched; what came of it is the
        # server's word, including the one an engine swapped mid-turn dropped.
        heard, sent = self.spoke({"events": ["laugh"], "eventsDropped": ["cough"]})
        self.assertEqual(sent["body"]["text"], "Hello")
        self.assertEqual(heard["events"], ["laugh"])
        self.assertEqual(heard["events_dropped"], ["cough"])

    def test_a_count_that_is_not_a_list_is_not_reported(self):
        heard, _ = self.spoke({"events": "laugh", "eventsDropped": None})
        self.assertNotIn("events", heard)
        self.assertNotIn("events_dropped", heard)

    def test_the_other_road_never_reaches_the_speak_server(self):
        brain.write_voice(backend="openai")
        with patch("urllib.request.urlopen", side_effect=AssertionError("spoke anyway")):
            heard = brain.say_aloud("Hello")
        self.assertFalse(heard["spoken"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
