# -*- coding: utf-8 -*-
"""Разовая операция освобождения слотов токенов VK.

Операция необратима для всех, кто ходит тем же client_id (у нас это агентство-подрядчик),
поэтому тесты стерегут именно защиту от случайного запуска, а не «счастливый путь».
"""
import os
from unittest.mock import patch

import pytest

from scripts import free_vk_token_slots as m

CABINETS = [("cid-woman", "sec-woman"), ("cid-kids", "sec-kids")]


def test_find_cabinet_returns_matching_pair():
    assert m.find_cabinet("cid-kids", CABINETS) == ("cid-kids", "sec-kids")


def test_find_cabinet_rejects_unknown_id():
    """Неизвестный client_id — отказ. Тихий пропуск означал бы чистку не того кабинета."""
    with pytest.raises(RuntimeError, match="не найден"):
        m.find_cabinet("cid-alien", CABINETS)


def test_main_refuses_without_confirmation():
    """Нет слова-подтверждения → ни удаления, ни выпуска: сеть не трогаем вообще."""
    env = {"FREE_VK_CLIENT_ID": "cid-woman", "FREE_VK_CONFIRM": ""}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(m, "_delete_all_tokens") as delete, \
         patch.object(m, "_issue_token") as issue:
        with pytest.raises(RuntimeError, match="подтверждение не получено"):
            m.main()
    delete.assert_not_called()
    issue.assert_not_called()


def test_main_refuses_with_wrong_confirmation_word():
    env = {"FREE_VK_CLIENT_ID": "cid-woman", "FREE_VK_CONFIRM": "да"}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(m, "_delete_all_tokens") as delete:
        with pytest.raises(RuntimeError, match="подтверждение не получено"):
            m.main()
    delete.assert_not_called()


def test_main_refuses_without_client_id():
    env = {"FREE_VK_CLIENT_ID": "", "FREE_VK_CONFIRM": m.CONFIRM_WORD}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(m, "_delete_all_tokens") as delete:
        with pytest.raises(RuntimeError, match="FREE_VK_CLIENT_ID"):
            m.main()
    delete.assert_not_called()


def test_main_deletes_then_immediately_issues_and_saves():
    """Порядок обязателен: сначала чистка, СРАЗУ следом выпуск — чтобы освободившийся
    слот не перехватил чужой процесс. И токен обязан сохраниться вместе с refresh."""
    calls = []
    env = {"FREE_VK_CLIENT_ID": "cid-woman", "FREE_VK_CONFIRM": m.CONFIRM_WORD}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(m, "_cabinets", return_value=CABINETS), \
         patch.object(m, "_delete_all_tokens", side_effect=lambda *a: calls.append("delete")), \
         patch.object(m, "_issue_token",
                      side_effect=lambda *a: (calls.append("issue"),
                                              {"access_token": "t", "refresh_token": "r",
                                               "expires_in": 86400})[1]), \
         patch.object(m, "_save_from_response",
                      side_effect=lambda *a: calls.append("save")) as save:
        assert m.main() == 0

    assert calls == ["delete", "issue", "save"]
    assert save.call_args[0][0] == "cid-woman"
    assert save.call_args[0][1]["refresh_token"] == "r"


def test_main_targets_only_requested_cabinet():
    """Чистим ровно тот кабинет, который назвали, — секрет соседнего не должен уехать в запрос."""
    env = {"FREE_VK_CLIENT_ID": "cid-kids", "FREE_VK_CONFIRM": m.CONFIRM_WORD}
    with patch.dict(os.environ, env, clear=False), \
         patch.object(m, "_cabinets", return_value=CABINETS), \
         patch.object(m, "_delete_all_tokens") as delete, \
         patch.object(m, "_issue_token", return_value={"access_token": "t", "refresh_token": "r"}), \
         patch.object(m, "_save_from_response"):
        m.main()
    delete.assert_called_once_with("cid-kids", "sec-kids")
