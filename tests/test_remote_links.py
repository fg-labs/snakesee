"""Tests for AWS console / CloudWatch deep-link builders."""

from snakesee.remote_links import batch_console_url
from snakesee.remote_links import batch_job_id_from
from snakesee.remote_links import cloudwatch_url
from snakesee.remote_links import region_from_arn

ARN = "arn:aws:batch:us-east-1:123456789012:job/12ab34cd-5678-90ef-ghij-klmnopqrstuv"
BARE_ID = "12ab34cd-5678-90ef-ghij-klmnopqrstuv"


class TestBatchJobIdFrom:
    """Extracting the bare Batch job id from an ARN or id."""

    def test_extracts_from_arn(self) -> None:
        assert batch_job_id_from(ARN) == BARE_ID

    def test_passes_through_bare_id(self) -> None:
        assert batch_job_id_from(BARE_ID) == BARE_ID

    def test_none_for_empty(self) -> None:
        assert batch_job_id_from(None) is None
        assert batch_job_id_from("") is None

    def test_strips_surrounding_whitespace(self) -> None:
        # Trailing/leading whitespace must not leak into the id (and thus the URL).
        assert batch_job_id_from(f"  {ARN}  ") == BARE_ID
        assert batch_job_id_from(f" {BARE_ID} ") == BARE_ID
        assert batch_job_id_from("   ") is None


class TestRegionFromArn:
    """Deriving the region from a Batch ARN."""

    def test_region_from_arn(self) -> None:
        assert region_from_arn(ARN) == "us-east-1"

    def test_none_for_bare_id(self) -> None:
        assert region_from_arn(BARE_ID) is None

    def test_none_for_none(self) -> None:
        assert region_from_arn(None) is None


class TestBatchConsoleUrl:
    """Building the AWS Batch job-detail console URL."""

    def test_url_with_explicit_region(self) -> None:
        url = batch_console_url(BARE_ID, region="eu-west-1")
        assert url == (
            "https://eu-west-1.console.aws.amazon.com/batch/home"
            f"?region=eu-west-1#jobs/detail/{BARE_ID}"
        )

    def test_region_derived_from_arn(self) -> None:
        # No explicit region, but the ARN carries it.
        url = batch_console_url(ARN)
        assert url is not None
        assert "region=us-east-1" in url
        assert f"jobs/detail/{BARE_ID}" in url

    def test_none_when_no_region_available(self) -> None:
        # Bare id with no region anywhere -> cannot build a console URL.
        assert batch_console_url(BARE_ID) is None

    def test_none_for_missing_id(self) -> None:
        assert batch_console_url(None, region="us-east-1") is None


class TestCloudwatchUrl:
    """Building the CloudWatch log-stream URL."""

    def test_url_default_log_group(self) -> None:
        url = cloudwatch_url("MyJobDef/default/abc123", region="us-east-1")
        assert url is not None
        assert url.startswith("https://us-east-1.console.aws.amazon.com/cloudwatch/home")
        assert "region=us-east-1" in url
        # The default Batch log group, url-encoded into the CloudWatch fragment.
        assert "$252Faws$252Fbatch$252Fjob" in url

    def test_slashes_in_stream_are_double_encoded(self) -> None:
        # Batch stream names contain "/" — each must become "$252F" in the fragment.
        url = cloudwatch_url("JobDef/default/abc123", region="us-east-1")
        assert url is not None
        assert "JobDef$252Fdefault$252Fabc123" in url

    def test_none_without_region(self) -> None:
        assert cloudwatch_url("stream", region=None) is None

    def test_none_without_stream(self) -> None:
        assert cloudwatch_url(None, region="us-east-1") is None
