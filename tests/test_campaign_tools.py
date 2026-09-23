"""The campaign as the model and the web console reach it."""

from __future__ import annotations

import time

import pytest

from tests.conftest import ROOT
from tests.test_campaign import VASP_FILE
from tests.test_ui import call, ui  # noqa: F401  (fixture)
from vaspilot.core.errors import ValidationError

RECIPE = {"functional": "PBE", "stages": ["band", "dos"],
          "resources": {"ntasks": 16, "walltime": "12:00:00"}}


@pytest.fixture()
def tools(app_with_fake, tmp_path):
    app, transport = app_with_fake
    vasp = tmp_path / "Si.vasp"
    vasp.write_text(VASP_FILE, encoding="utf-8")
    return app, app.registry(), transport.state, str(vasp)


def doctor(registry):
    return registry.dispatch("vaspkit_doctor", {"server": "cl9"})


class TestTools:
    def test_all_five_are_registered(self, tools):
        _, registry, _, _ = tools
        assert {"vaspkit_doctor", "campaign_plan", "campaign_start",
                "campaign_status", "campaign_abort"} <= set(registry.names())
        assert registry.get("campaign_start").kind == "write"
        assert registry.get("campaign_plan").kind == "read"

    def test_doctor_remembers_a_ready_server(self, tools):
        app, registry, _, _ = tools
        result = doctor(registry)
        assert result["ready"] is True
        assert result["mode"] == "stdin"
        from vaspilot.workflow.campaign import profile_store
        assert profile_store(app.config).get("cl9")["ready"] is True

    def test_plan_without_recipe_returns_the_annotation(self, tools):
        _, registry, _, vasp = tools
        result = registry.dispatch("campaign_plan", {"vasp_path": vasp})
        assert result["stage"] == "needs_recipe"
        assert "能带" in result["annotation"]
        assert result["structure"]["formula"] == "Si2"
        assert result["vaspkit_ready"] is False
        assert "vaspkit_doctor" in result["next"]
        assert "stages" in result["recipe_fields"]

    def test_plan_needs_the_probe_first(self, tools):
        _, registry, _, vasp = tools
        with pytest.raises(ValidationError, match="vaspkit_doctor"):
            registry.dispatch("campaign_plan",
                              {"vasp_path": vasp, "recipe": RECIPE})

    def test_plan_shows_every_stage(self, tools):
        _, registry, state, vasp = tools
        doctor(registry)
        result = registry.dispatch("campaign_plan",
                                   {"vasp_path": vasp, "recipe": RECIPE})
        assert result["stage"] == "planned"
        assert [s["name"] for s in result["stages"]] == [
            "relax", "static", "band", "dos"]
        band = next(s for s in result["stages"] if s["name"] == "band")
        assert band["settings"]["ICHARG"] == "11"
        assert "303" in band["kpoints"]
        assert state.jobs["cl9"] == []

    def test_missing_file_is_refused(self, tools):
        _, registry, _, vasp = tools
        with pytest.raises(ValidationError, match="not a file"):
            registry.dispatch("campaign_plan", {"vasp_path": vasp + ".nope"})

    def test_start_waits_for_a_human_in_confirm_mode(self, tools):
        app, registry, state, vasp = tools
        app.config.set_agent_submit_mode("confirm")
        doctor(registry)
        result = registry.dispatch("campaign_start",
                                   {"vasp_path": vasp, "recipe": RECIPE})
        assert result["status"] == "awaiting_approval"
        assert "我的计算" in result["message"]
        shown = registry.dispatch("campaign_status",
                                  {"campaign_id": result["campaign_id"]})
        assert shown["status"] == "awaiting_approval"
        assert state.jobs["cl9"] == []

    def test_start_runs_straight_away_in_auto_mode(self, tools):
        app, registry, _, vasp = tools
        app.config.set_agent_submit_mode("auto")
        doctor(registry)
        result = registry.dispatch("campaign_start",
                                   {"vasp_path": vasp, "recipe": RECIPE})
        assert result["status"] == "running"

    def test_start_needs_a_recipe(self, tools):
        _, registry, _, vasp = tools
        with pytest.raises(ValidationError, match="recipe"):
            registry.dispatch("campaign_start", {"vasp_path": vasp})

    def test_status_lists_campaigns(self, tools):
        _, registry, _, vasp = tools
        doctor(registry)
        started = registry.dispatch("campaign_start",
                                    {"vasp_path": vasp, "recipe": RECIPE})
        listing = registry.dispatch("campaign_status", {})
        assert [c["campaign_id"] for c in listing["campaigns"]] == \
            [started["campaign_id"]]

    def test_abort(self, tools):
        app, registry, _, vasp = tools
        app.config.set_agent_submit_mode("auto")
        doctor(registry)
        started = registry.dispatch("campaign_start",
                                    {"vasp_path": vasp, "recipe": RECIPE})
        result = registry.dispatch("campaign_abort",
                                   {"campaign_id": started["campaign_id"]})
        assert result["status"] == "aborted"


class TestConsole:
    def test_attach_keeps_the_exact_file(self, ui):  # noqa: F811
        result = call(ui, "vasp.attach", {"name": "Si.vasp", "text": VASP_FILE})
        assert result["ok"] is True
        assert result["formula"] == "Si2"
        assert "能带" in result["annotation"]
        from pathlib import Path
        assert Path(result["path"]).read_text(encoding="utf-8") == VASP_FILE

    def test_attach_refuses_a_path_as_the_name(self, ui):  # noqa: F811
        result = call(ui, "vasp.attach", {"name": "a/../../x;rm", "text": VASP_FILE})
        assert result["ok"] is False

    def test_attach_refuses_something_that_is_not_a_structure(self, ui):  # noqa: F811
        result = call(ui, "vasp.attach", {"name": "notes.vasp",
                                          "text": "hello\nworld\n"})
        assert result["ok"] is False

    def test_approve_in_the_console_starts_the_chain(self, ui):  # noqa: F811
        app = ui["app"]
        app.config.set_agent_submit_mode("confirm")
        registry = app.registry()
        registry.dispatch("vaspkit_doctor", {"server": "cl9"})
        attached = call(ui, "vasp.attach", {"name": "Si.vasp", "text": VASP_FILE})
        started = registry.dispatch("campaign_start", {
            "vasp_path": attached["path"], "recipe": RECIPE})

        listing = call(ui, "campaign.list")
        row = next(c for c in listing["campaigns"]
                   if c["campaign_id"] == started["campaign_id"])
        assert row["status"] == "awaiting_approval"
        assert row["stages"][0]["settings"]["ISIF"] == "3"

        approved = call(ui, "campaign.approve", {"id": started["campaign_id"]})
        assert approved["ok"] is True and approved["status"] == "running"
        # the approval kicks the ticker; the first stage is submitted from it
        deadline = time.time() + 10
        while time.time() < deadline and not ui["state"].jobs["cl9"]:
            time.sleep(0.1)
        assert len(ui["state"].jobs["cl9"]) == 1

        again = call(ui, "campaign.approve", {"id": started["campaign_id"]})
        assert again["ok"] is False

    def test_reject_in_the_console_runs_nothing(self, ui):  # noqa: F811
        app = ui["app"]
        app.config.set_agent_submit_mode("confirm")
        registry = app.registry()
        registry.dispatch("vaspkit_doctor", {"server": "cl9"})
        attached = call(ui, "vasp.attach", {"name": "Si.vasp", "text": VASP_FILE})
        started = registry.dispatch("campaign_start", {
            "vasp_path": attached["path"], "recipe": RECIPE})
        rejected = call(ui, "campaign.reject", {"id": started["campaign_id"]})
        assert rejected["status"] == "rejected"
        time.sleep(0.3)
        assert ui["state"].jobs["cl9"] == []

    def test_unknown_campaign_is_an_error_not_a_crash(self, ui):  # noqa: F811
        result = call(ui, "campaign.abort", {"id": "0" * 16})
        assert result["ok"] is False
        assert "not found" in result["error"]["message"]


class TestLibrarySetting:
    LIB = "/data/pot/PBE.54"

    def test_set_get_clear(self, tools):
        app, _, _, _ = tools
        assert app.config.potcar_library("cl9") == ""
        assert app.config.set_potcar_library("cl9", self.LIB + "/") == self.LIB
        assert app.config.potcar_library("cl9") == self.LIB
        assert app.config.potcar_library("pbs1") == ""
        app.config.set_potcar_library("cl9", "")
        assert app.config.potcar_library("cl9") == ""

    @pytest.mark.parametrize("path", ["pot/PBE", "/a/../b", "/a;rm", "/a b"])
    def test_bad_paths_are_refused(self, tools, path):
        app, _, _, _ = tools
        with pytest.raises(ValidationError, match="absolute path"):
            app.config.set_potcar_library("cl9", path)

    def test_doctor_probes_the_configured_library(self, tools):
        app, registry, _, _ = tools
        app.config.set_potcar_library("cl9", self.LIB)
        result = doctor(registry)
        assert result["ready"] is True
        assert result["potcar_library"] == self.LIB
        assert result["library"]["path"] == self.LIB

    def test_doctor_reports_a_missing_library(self, tools):
        app, registry, _, _ = tools
        app.config.set_potcar_library("cl9", "/data/missing/PBE")
        result = doctor(registry)
        assert result["ready"] is False

    def test_changing_the_library_needs_a_new_probe(self, tools):
        app, registry, _, vasp = tools
        app.config.set_potcar_library("cl9", self.LIB)
        doctor(registry)
        registry.dispatch("campaign_plan", {"vasp_path": vasp, "recipe": RECIPE})
        app.config.set_potcar_library("cl9", "/data/pot/other")
        peek = registry.dispatch("campaign_plan", {"vasp_path": vasp})
        assert peek["vaspkit_ready"] is False
        with pytest.raises(ValidationError, match="vaspkit_doctor again"):
            registry.dispatch("campaign_plan", {"vasp_path": vasp, "recipe": RECIPE})

    def test_console_settings_flow(self, ui):  # noqa: F811
        rows = call(ui, "vaspkit.settings")["servers"]
        assert rows[0]["server"] == "cl9" and rows[0]["probed"] is False

        bad = call(ui, "vaspkit.save", {"server": "cl9", "potcar_library": "pot"})
        assert bad["ok"] is False

        saved = call(ui, "vaspkit.save", {"server": "cl9",
                                          "potcar_library": self.LIB})
        assert saved["potcar_library"] == self.LIB and saved["probed"] is False

        probed = call(ui, "vaspkit.doctor", {"server": "cl9"})
        assert probed["ready"] is True and probed["stale"] is False
        assert probed["library"]["path"] == self.LIB

        moved = call(ui, "vaspkit.save", {"server": "cl9",
                                          "potcar_library": "/data/pot/other"})
        assert moved["stale"] is True

    def test_console_refuses_unknown_servers(self, ui):  # noqa: F811
        result = call(ui, "vaspkit.save", {"server": "nope",
                                           "potcar_library": self.LIB})
        assert result["ok"] is False
