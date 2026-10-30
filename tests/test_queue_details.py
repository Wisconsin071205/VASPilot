"""Real Torque headers and older PBS summaries must retain their meaning."""
from rsci.gateway.client import _parse_metric_sections


def test_torque_node_column_does_not_shift_counts_or_state():
    raw = """pbs
server: admin
Queue            Memory CPU Time Walltime Node  Run Que Lm  State
---------------- ------ -------- -------- ----  --- --- --  -----
short              --      --    06:00:00     5   1   4 20   E R
normal             --      --    36:00:00     5   1   2 20   E R
                                               ----- -----
                                                   2     6"""
    queue = _parse_metric_sections({"sched": raw})["queue"]
    assert len(queue["partitions"]) == 2
    short = queue["partitions"][0]
    assert short["run"] == 1 and short["queued"] == 4
    assert short["nodes_limit"] == 5 and short["lm"] == "20"
    assert short["state"] == "E R" and short["walltime_limit"] == "06:00:00"
    assert queue["source_command"] == "qstat -q"
    assert "CPU Time Walltime Node" in queue["raw"]


def test_pbs_without_node_column_and_stopped_queue():
    queue = _parse_metric_sections({"sched": "pbs\nQueue Memory CPU Time Walltime Run Que Lm State\nshort -- -- 01:00:00 3 7 -- D S"})["queue"]
    row = queue["partitions"][0]
    assert (row["run"], row["queued"], row["state"]) == (3, 7, "D S")
    assert row["nodes_limit"] is None and row["lm"] == "--"


def test_unknown_format_preserves_evidence_without_inventing_counts():
    queue = _parse_metric_sections({"sched": "pbs\nshort -- -- 01:00:00 5 1 4 20 E R"})["queue"]
    assert not queue["partitions"] and queue["parse_warning"]
    assert "short" in queue["raw"]


def test_slurm_provenance_and_counts_remain_available():
    queue = _parse_metric_sections({"sched": "slurm\nnormal|up|2|10/20/2/32"})["queue"]
    row = queue["partitions"][0]
    assert (row["cpus_alloc"], row["cpus_idle"], row["cpus_total"]) == (10, 20, 32)
    assert queue["source_command"].startswith("sinfo")
    assert queue["raw"] == "normal|up|2|10/20/2/32"
