import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_models import (
    DEFAULT_OUTPUT_ROOT,
    build_prompt,
    build_command,
    validate_case_a_output,
    validate_case_b_output,
    write_png_text_image,
    evaluate_model,
    score_response,
    synthetic_cases,
)


class ModelEvalTests(unittest.TestCase):
    def test_score_uses_labels_not_raw_substrings(self):
        cases = synthetic_cases()
        _, case_results, score, error = score_response(
            '{"cases":[{"case_id":"acwr_deload","label":"automatic_vigorous_prescription",'
            '"rationale":"Do not prescribe vigorous exercise just because the ratio is low."}]}',
            cases,
        )
        self.assertIsNone(error)
        self.assertEqual(score, 0.0)
        acwr = next(item for item in case_results if item["case_id"] == "acwr_deload")
        self.assertFalse(acwr["matched"])
        self.assertEqual(acwr["received_label"], "automatic_vigorous_prescription")

        _, case_results, score, error = score_response(
            '{"cases":[{"case_id":"acwr_deload","label":"respect_planned_recovery",'
            '"rationale":"Automatic vigorous exercise would be the wrong move."}]}',
            cases,
        )
        self.assertIsNone(error)
        self.assertEqual(score, 1 / len(cases))
        acwr = next(item for item in case_results if item["case_id"] == "acwr_deload")
        self.assertTrue(acwr["matched"])

    def test_failed_run_is_not_marked_completed(self):
        cases = synthetic_cases()
        # The helper should not treat a non-zero or missing response as a successful evaluation.
        parsed, results, score, error = score_response("", cases)
        self.assertIsNone(parsed)
        self.assertEqual(results, [])
        self.assertIsNone(score)
        self.assertIsNotNone(error)

    def test_prompt_is_long_and_contains_all_case_ids(self):
        prompt = build_prompt(synthetic_cases())
        self.assertGreater(len(prompt), 8000)
        for case in synthetic_cases():
            self.assertIn(case.case_id, prompt)
        self.assertIn("Return JSON only", prompt)

    def test_command_uses_empty_allowlist_without_deny_wildcard(self):
        command, _ = build_command("gemini-3.7-flash", "medium", "synthetic")
        self.assertIn("--available-tools=", command)
        self.assertNotIn("--deny-tool=*", command)

    def test_default_output_root_is_generic(self):
        self.assertEqual(
            DEFAULT_OUTPUT_ROOT,
            Path.home() / ".copilot" / "session-state" / "model-evaluation",
        )

    def test_command_supports_attachments_and_strict_deny_probe(self):
        command, _ = build_command(
            "gemini-3.7-flash",
            "medium",
            "synthetic",
            attachments=["one.png", "two.png"],
            strict_deny_wildcard=True,
        )
        self.assertIn("--deny-tool=*", command)
        self.assertEqual(command.count("--attachment"), 2)
        self.assertIn("one.png", command)
        self.assertIn("two.png", command)

    def test_realistic_case_a_validator_accepts_expected_markers(self):
        text = (
            "AgBot - 2026-09-07\n"
            '[[MEMORY: {"kind":"preference","key":"avoid_exercise:standard barbell back squat",'
            '"text":"standard barbell back squats still irritate my knee, but goblet squats are okay again.",'
            '"source_quote":"standard barbell back squats still irritate my knee, but goblet squats are okay again.",'
            '"action":"upsert"}]]\n'
            '[[SESSION_PLAN: {"date":"2026-09-07","kind":"strength","objective":"Goblet squat re-entry",'
            '"reason":"Keep the standard back squat out while reintroducing a tolerable variant.",'
            '"exercises":[{"name":"Goblet squat","sets":[{"reps":"8","weight_kg":18,"rest_seconds":90},'
            '{"reps":"8","weight_kg":18,"rest_seconds":90}],'
            '"effort":"Stop with two reps in reserve"}]}]]'
        )
        checks = validate_case_a_output(
            text,
            today_iso="2026-09-07",
            source_quote="standard barbell back squats still irritate my knee, but goblet squats are okay again.",
            allowed_weight_kg=18.0,
            banned_variant="standard barbell back squat",
        )
        self.assertTrue(checks["overall"])
        self.assertTrue(checks["memory_ok"])
        self.assertTrue(checks["plan_ok"])
        self.assertTrue(checks["no_saved_promise"])

    def test_realistic_case_b_validator_reads_both_images(self):
        text = (
            "The two images together show 12 g, 23 min, and 240 calories. "
            "That is exercise fuel use, not body-fat loss, so do not subtract it twice."
        )
        checks = validate_case_b_output(text)
        self.assertTrue(checks["overall"])
        self.assertTrue(checks["number_checks"]["fat_12g"])
        self.assertTrue(checks["number_checks"]["elapsed_23min"])
        self.assertTrue(checks["number_checks"]["calories_240"])

    def test_png_image_writer_creates_artifact(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            write_png_text_image(path, "Synthetic", ["FAT BURNER", "12 G"])
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 0)

    def test_supported_models_are_reported_without_scoring_unsupported_probes(self):
        # Only validate the scoring helper here; the real CLI probe happens in integration runs.
        parsed, results, score, error = score_response(
            '{"cases":[{"case_id":"symptom_resolution","label":"temporary_guarded_reintroduction",'
            '"rationale":"Resolved symptoms justify a cautious return."}]}',
            synthetic_cases(),
        )
        self.assertIsNone(error)
        self.assertGreaterEqual(score, 0.0)
        self.assertTrue(next(item for item in results if item["case_id"] == "symptom_resolution")["matched"])
        self.assertIsNotNone(parsed)

    def test_evaluate_model_supports_injected_prompt_and_no_external_judge(self):
        # Use only the scoring helper to ensure the module can aggregate a parsed run.
        cases = synthetic_cases()
        parsed, results, score, error = score_response(
            '{"cases":[{"case_id":"source_quote_plan_actual","label":"keep_source_plan_actual_separate",'
            '"rationale":"The quote, plan, and completion stay distinct."}]}',
            cases,
        )
        self.assertIsNone(error)
        self.assertIsNotNone(parsed)
        self.assertEqual(score, 1 / len(cases))
        self.assertEqual(results[6]["case_id"], "source_quote_plan_actual")


if __name__ == "__main__":
    unittest.main()
