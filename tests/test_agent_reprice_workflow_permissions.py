# -*- coding: utf-8 -*-
"""agent-reprice.yml — воркфлоу с DATABASE_URL и правом на запись в
edu_agent_actions (--apply) обязан объявлять permissions явно. Без блока
GITHUB_TOKEN по умолчанию получает права репозитория (у публичных репо это
обычно read у contents, но зависит от настроек организации и может измениться
без ведома этого файла) — минимальный принцип требует явного "{}"."""
import pathlib

import yaml

WORKFLOW = (pathlib.Path(__file__).resolve().parent.parent
           / ".github" / "workflows" / "agent-reprice.yml")


def test_agent_reprice_declares_permissions_block():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "permissions" in doc, (
        "agent-reprice.yml пишет в боевой журнал по DATABASE_URL — "
        "GITHUB_TOKEN обязан быть явно ограничен, а не унаследован от "
        "настроек организации")
    assert doc["permissions"] == {}
