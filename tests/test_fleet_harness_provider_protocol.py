"""Pure wire regressions from the indexed DeepSeek live response shape."""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import fleet_harness_provider_protocol as wire
import fleet_harness_mini as mini
from tests.test_fleet_harness_mini import response


class ProviderProtocolTests(unittest.TestCase):
    def test_output_profile_is_pinned_and_legacy_payloads_remain_8192(self):
        for version in (wire.LEGACY_VERSION,wire.INDEXED_VERSION):
            self.assertEqual(wire.payload([],[],version=version)["max_tokens"],8192)
        self.assertEqual(wire.payload([],[])["max_tokens"],8192)
        self.assertEqual(wire.payload([],[],version=wire.THINKING_32K_VERSION)["max_tokens"],32768)
        original=self.indexed();before=copy.deepcopy(original)
        self.assertEqual(wire.normalize(original,version=wire.THINKING_32K_VERSION),wire.normalize(original,version=wire.INDEXED_VERSION))
        self.assertEqual(original,before)
        original["choices"][0]["message"]["tool_calls"][0]["index"]=True
        with self.assertRaises(ValueError):wire.normalize(original,version=wire.THINKING_32K_VERSION)

    def indexed(self):
        value=response("pwd",identifier="first",content="")
        message=value["choices"][0]["message"]
        message["reasoning_content"]="synthetic reasoning"
        message["tool_calls"]+=response("ls",identifier="second")["choices"][0]["message"]["tool_calls"]
        for index,call in enumerate(message["tool_calls"]):call["index"]=index
        return value

    def test_indexed_response_projects_without_mutating_any_original_field(self):
        original=self.indexed();before=copy.deepcopy(original)
        projected=wire.normalize(original,version=wire.VERSION)
        expected=copy.deepcopy(original)
        for call in expected["choices"][0]["message"]["tool_calls"]:del call["index"]
        self.assertEqual(projected,expected)
        self.assertEqual(original,before)
        self.assertEqual(mini.validate_batch(projected,set()),[
            {"command":"pwd","tool_call_id":"first"},{"command":"ls","tool_call_id":"second"}])
        projected["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]="changed"
        self.assertEqual(original,before)

    def test_legacy_projection_still_rejects_indexed_original(self):
        original=self.indexed()
        self.assertEqual(wire.normalize(original,version=wire.LEGACY_VERSION),original)
        with self.assertRaises(ValueError):mini.validate_batch(wire.normalize(original,version=wire.LEGACY_VERSION),set())

    def test_pinned_algorithm_is_independent_of_current_default(self):
        original=self.indexed()
        with mock.patch.object(wire,"VERSION","future-default"):
            self.assertEqual(wire.normalize(original,version=wire.LEGACY_VERSION),original)
            self.assertEqual(len(mini.validate_batch(wire.normalize(original,version=wire.INDEXED_VERSION),set())),2)

    def test_unindexed_responses_keep_identical_v2_and_v3_behavior(self):
        original=response("pwd",content="")
        original["choices"][0]["message"]["content"]=None
        v2=wire.normalize(original,version=wire.LEGACY_VERSION)
        v3=wire.normalize(original,version=wire.VERSION)
        self.assertEqual(v2,v3)
        self.assertIsNone(original["choices"][0]["message"]["content"])
        self.assertEqual(mini.validate_batch(v3,set())[0]["command"],"pwd")

    def test_invalid_index_types_and_mixed_or_reordered_batches_are_rejected(self):
        for values in ((True,1),(0.0,1),(0,1.0),(-1,1),(0,0),(1,0),(0,2),("0",1),(None,1)):
            original=self.indexed()
            for call,index in zip(original["choices"][0]["message"]["tool_calls"],values):call["index"]=index
            before=copy.deepcopy(original)
            with self.subTest(values=values),self.assertRaises(ValueError):wire.normalize(original)
            self.assertEqual(original,before)
        original=self.indexed();del original["choices"][0]["message"]["tool_calls"][1]["index"]
        with self.assertRaises(ValueError):wire.normalize(original)

    def test_unknown_fields_duplicate_ids_and_mixed_submit_never_gain_authority(self):
        mutations=(lambda c:c[0].update(unrecognized=True),lambda c:c[1].update(id=c[0]["id"]),
                   lambda c:c[0]["function"].update(arguments='{"command":"'+mini.SENTINEL+'"}'))
        for mutate in mutations:
            original=self.indexed();mutate(original["choices"][0]["message"]["tool_calls"])
            with self.assertRaises(ValueError):mini.validate_batch(wire.normalize(original),set())
        with self.assertRaises(ValueError):mini.validate_batch(wire.normalize(self.indexed()),{"first"})

    def test_unknown_versions_are_rejected_everywhere(self):
        for version in (None,True,[],"deepseek-mini-wire-v999"):
            for action in (lambda:wire.normalize(self.indexed(),version=version),
                           lambda:wire.messages([],version=version),lambda:wire.payload([],[],version=version)):
                with self.subTest(version=version),self.assertRaises(ValueError):action()


if __name__=="__main__":unittest.main()
