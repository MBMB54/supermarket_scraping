import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

spec = importlib.util.spec_from_file_location(
    "handler", Path(__file__).resolve().parents[1] / "lambda" / "handler.py"
)
handler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)


def submitted(retailer: str) -> list[dict]:
    batch = MagicMock()
    batch.submit_job.return_value = {"jobId": "j"}
    handler.submit_scraper_chunks(batch, retailer, f"{retailer}_api.py")
    return [c.kwargs for c in batch.submit_job.call_args_list]


def test_tesco_chunks_get_reduced_resources_and_longer_timeout():
    calls = submitted("tesco")
    assert len(calls) == handler.TOTAL_CHUNKS
    for kwargs in calls:
        assert kwargs["containerOverrides"]["resourceRequirements"] == (
            handler.TESCO_RESOURCE_REQUIREMENTS
        )
        assert kwargs["timeout"] == {
            "attemptDurationSeconds": handler.TESCO_ATTEMPT_DURATION_SECONDS
        }


def test_other_retailers_keep_job_definition_resources():
    for retailer in ("aldi", "dunnes", "supervalu"):
        for kwargs in submitted(retailer):
            assert "resourceRequirements" not in kwargs["containerOverrides"]
            assert "timeout" not in kwargs
