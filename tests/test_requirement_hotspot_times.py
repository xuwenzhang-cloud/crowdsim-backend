"""Hotspot requirement validation and persistence; no model or simulator I/O."""
from copy import deepcopy
import tempfile
import unittest

from crowdsim.domain.requirement_spec import RequirementSpec, RequirementValidationError
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from tests.test_requirement_location_v2 import located_requirement


class RequirementHotspotTimesTests(unittest.TestCase):
    def requirement(self, start=900, end=None, duration=60):
        raw = located_requirement(True)
        raw.update(research={"problem_type": 1}, experiment={"duration_seconds": duration, "repetitions": 3})
        raw["scenario"].update(hotspot_start_seconds=start, hotspot_end_seconds=end)
        return raw

    def test_windows_are_independent_of_the_observation_duration_and_not_clipped(self):
        for start, end, duration in ((0, .5, .1), (900, 1200, 60), (900.5, None, 60), (900, None, 1800)):
            with self.subTest(start=start, end=end, duration=duration):
                raw = self.requirement(start, end, duration)
                before = deepcopy(raw)
                self.assertEqual(before, RequirementSpec.parse(raw).payload)
                self.assertEqual(before, raw)

    def test_invalid_values_and_reversed_windows_are_rejected(self):
        for field in ("hotspot_start_seconds", "hotspot_end_seconds"):
            for value in (-1, True, "600", {}, [], float("nan"), float("inf"), 10 ** 400):
                with self.subTest(field=field, value=str(value)):
                    raw = self.requirement()
                    raw["scenario"][field] = value
                    with self.assertRaisesRegex(RequirementValidationError, "non-negative seconds"):
                        RequirementSpec.parse(raw)
        with self.assertRaises(RequirementValidationError):
            RequirementSpec.parse(self.requirement(None))
        for end in (899, 900):
            with self.subTest(end=end), self.assertRaisesRegex(RequirementValidationError, "later than"):
                RequirementSpec.parse(self.requirement(900, end))

    def test_numeric_and_open_ends_persist_as_distinct_immutable_requirement_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = RequirementRepository(directory)
            numeric = repository.create(RequirementSpec.parse(self.requirement(end=1200)))
            open_end = repository.create(RequirementSpec.parse(self.requirement()))
            self.assertNotEqual(numeric["requirement_id"], open_end["requirement_id"])
            reopened = RequirementRepository(directory)
            self.assertEqual(1200, reopened.load(numeric["requirement_id"])["requirement"]["scenario"]["hotspot_end_seconds"])
            restored = reopened.load(open_end["requirement_id"])["requirement"]
            self.assertIsNone(restored["scenario"]["hotspot_end_seconds"])
            self.assertEqual(900, restored["scenario"]["hotspot_start_seconds"])
            self.assertEqual(60, restored["experiment"]["duration_seconds"])
            self.assertEqual(open_end, reopened.create(RequirementSpec.parse(self.requirement())))

    def test_omitting_the_optional_end_does_not_invent_a_preset_end(self):
        raw = self.requirement()
        del raw["scenario"]["hotspot_end_seconds"]
        self.assertNotIn("hotspot_end_seconds", RequirementSpec.parse(raw).payload["scenario"])
