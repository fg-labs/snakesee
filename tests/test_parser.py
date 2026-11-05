from __future__ import annotations

from snakesee.model import JobStatus
from snakesee.model import RunStatus
from snakesee.parser import LogParser
from snakesee.parser import parse_timestamp

from .conftest import LSF_LOG


def parse_fixture() -> LogParser:
    p = LogParser()
    p.feed(LSF_LOG.read_text().splitlines())
    return p


def test_lsf_fixture_jobs() -> None:
    p = parse_fixture()
    assert set(p.jobs) == {3233, 3481, 3775, 3709, 3555}
    done = p.jobs[3233]
    assert done.status is JobStatus.DONE
    assert done.rule == "run_total_model"
    assert done.wildcards == "tissue_real=Adipose, chromosome=11, chunk_num=2"
    assert done.threads == 4
    assert done.external_id == "101867798"
    assert done.cluster_log is not None
    assert done.cluster_log.endswith(
        "rule_run_total_model/tissue_real=Adipose..chromosome=11..chunk_num=2/"
        "45902017-3b80-4b48-bba3-999360bbce28.out"
    )
    assert done.submit_time == parse_timestamp("Thu Nov  5 12:16:00 2026")
    assert done.end_time == parse_timestamp("Thu Nov  5 12:30:00 2026")

    assert p.jobs[3481].status is JobStatus.ACTIVE
    assert p.jobs[3775].status is JobStatus.ACTIVE

    failed = p.jobs[3555]
    assert failed.status is JobStatus.FAILED
    assert failed.error_message == "LSF-job '101867877' failed, LSF status is: 'EXIT'"
    assert failed.end_time == parse_timestamp("Thu Nov  5 12:39:35 2026")
    # The error block's log: is the cluster log we already know; don't treat it as a rule log.
    assert failed.rule_logs == []


def test_lsf_fixture_summary() -> None:
    s = parse_fixture().summary()
    assert s.status is RunStatus.FAILED
    assert (s.steps_done, s.steps_total) == (1, 6)
    assert (s.n_active, s.n_done, s.n_failed) == (2, 1, 2)
    assert s.snakemake_version == "9.26.1"


def test_line_at_a_time_matches_bulk() -> None:
    bulk = parse_fixture()
    p = LogParser()
    for line in LSF_LOG.read_text().splitlines():
        p.feed([line])
    assert {k: v.copy() for k, v in p.jobs.items()} == {k: v.copy() for k, v in bulk.jobs.items()}
    assert p.summary() == bulk.summary()


def test_total_from_job_stats_before_progress() -> None:
    p = LogParser()
    p.feed(LSF_LOG.read_text().splitlines()[:30])
    assert p.rule_counts["run_total_model"] == 3
    assert p.summary().steps_total == 6


def test_drain_changes_only_reports_new_changes() -> None:
    p = LogParser()
    lines = LSF_LOG.read_text().splitlines()
    finished_at = next(i for i, ln in enumerate(lines) if ln.startswith("Finished"))
    p.feed(lines[:finished_at])
    assert len(p.drain_changes()) == 5
    assert p.drain_changes() == []
    p.feed(lines[finished_at : finished_at + 1])
    changed = p.drain_changes()
    assert [j.jobid for j in changed] == [3233]
    assert changed[0].status is JobStatus.DONE


LOCAL_LOG = """\
Job stats:
job      count
-----  -------
a            2
total        2

[Mon Dec 15 22:34:30 2025]
localrule a:
    output: out/1.txt
    log: logs/a/1.log, logs/a/1.err
    jobid: 1
    wildcards: n=1
    resources: tmpdir=/tmp

[Mon Dec 15 22:34:31 2025]
Finished job 1.
1 of 2 steps (50%) done
Submitted job 2 with external jobid 'Job <555> is submitted to queue <normal>.'.
"""


def test_local_rule_logs_and_snakemake8_finished() -> None:
    p = LogParser()
    p.feed(LOCAL_LOG.splitlines())
    job = p.jobs[1]
    assert job.rule == "a"
    assert job.status is JobStatus.DONE
    assert job.rule_logs == ["logs/a/1.log", "logs/a/1.err"]
    assert p.summary().status is RunStatus.RUNNING


def test_generic_submit_extracts_bsub_id() -> None:
    p = LogParser()
    p.feed(
        [
            "rule b:",
            "    jobid: 2",
            "Submitted job 2 with external jobid 'Job <555> is submitted to queue <normal>.'.",
            "rule b:",
            "    jobid: 3",
            "Submitted job 3 with external jobid '1234;cluster'.",
        ]
    )
    assert p.jobs[2].external_id == "555"
    assert p.jobs[3].external_id == "1234"


def test_retry_reactivates_failed_job() -> None:
    p = LogParser()
    p.feed(["rule b:", "    jobid: 7", "Error in rule b:", "    jobid: 7", "    message: boom"])
    assert p.jobs[7].status is JobStatus.FAILED
    p.feed(["rule b:", "    jobid: 7", "x"])
    job = p.jobs[7]
    assert job.status is JobStatus.ACTIVE
    assert job.attempts == 2
    assert job.error_message == ""
    s = p.summary()
    assert (s.n_active, s.n_failed) == (1, 0)


def test_nothing_to_be_done() -> None:
    p = LogParser()
    p.feed(["Building DAG of jobs...", "Nothing to be done (all requested files are present)."])
    assert p.summary().status is RunStatus.NOTHING_TO_DO
