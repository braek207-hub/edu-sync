import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync.classify import detect_direction, detect_project


def test_detect_project_provuz():
    assert detect_project("provuz_postupi_spo_msk") == "provuz"
    assert detect_project("postupi_provuz_vpo") == "provuz"


def test_detect_project_vse():
    assert detect_project("vsekolledzhi_postupi_spo") == "vse"


def test_detect_project_vuz():
    assert detect_project("vuz_edunetwork_spo_search") == "vuz"


def test_detect_project_provuz_before_vuz():
    assert detect_project("provuz_vuz_spo") == "provuz"


def test_detect_project_unknown():
    assert detect_project("") == "unknown"
    assert detect_project("random_campaign_name") == "unknown"


def test_detect_direction_mti():
    assert detect_direction("vsekolledzhi_мти_search") == "mti"
    assert detect_direction("vuz_ mti _rsya") == "mti"


def test_detect_direction_ntb():
    assert detect_direction("provuz_нтб_search") == "ntb"


def test_detect_direction_med():
    assert detect_direction("vuz_медицина_rsya") == "med"
    assert detect_direction("provuz_мед_search") == "med"


def test_detect_direction_transfer():
    assert detect_direction("provuz_перевод_search") == "transfer"


def test_detect_direction_it():
    assert detect_direction("vuz_it_программирование") == "it"
    assert detect_direction("vuz_python_search") == "it"
    assert detect_direction("vuz_web_rsya") == "it"


def test_detect_direction_dist():
    assert detect_direction("vse_дистанц_search") == "dist"
    assert detect_direction("vuz_заочн_rsya") == "dist"
    assert detect_direction("provuz_ рф _search") == "dist"


def test_detect_direction_spo():
    assert detect_direction("vsekolledzhi_спо_msk_search") == "spo"


def test_detect_direction_vpo():
    assert detect_direction("vuz_впо_rsya") == "vpo"


def test_detect_direction_other():
    assert detect_direction("") == "other"
    assert detect_direction("неизвестная_кампания") == "other"
