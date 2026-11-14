"""共享配置层测试: 路径解析 / 容错读取 / 分层合并 / 缓存失效."""

from __future__ import annotations

import json

from tracker import config


def test_resolve_precedence_explicit_over_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    env_file = tmp_path / "env.json"
    default = tmp_path / "default.json"
    example = tmp_path / "example.json"
    monkeypatch.setenv("TEST_CONFIG", str(env_file))
    assert (
        config.resolve_config_path(explicit, "TEST_CONFIG", default, example)
        == explicit
    )


def test_resolve_env_over_default(tmp_path, monkeypatch):
    env_file = tmp_path / "env.json"
    default = tmp_path / "default.json"
    default.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TEST_CONFIG", str(env_file))
    assert (
        config.resolve_config_path(None, "TEST_CONFIG", default, None) == env_file
    )


def test_resolve_default_then_example(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CONFIG", raising=False)
    default = tmp_path / "default.json"
    example = tmp_path / "example.json"
    assert (
        config.resolve_config_path(None, "TEST_CONFIG", default, example) == example
    )
    default.write_text("{}", encoding="utf-8")
    assert (
        config.resolve_config_path(None, "TEST_CONFIG", default, example) == default
    )


def test_resolve_require_exists_falls_through_missing_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_CONFIG", raising=False)
    default = tmp_path / "default.json"
    default.write_text("{}", encoding="utf-8")
    example = tmp_path / "example.json"
    missing = tmp_path / "missing.json"
    assert (
        config.resolve_config_path(
            missing, "TEST_CONFIG", default, example, require_exists=True
        )
        == default
    )


def test_load_missing_corrupt_and_non_object(tmp_path):
    assert config.load_json_object(tmp_path / "nope.json") == {}

    broken = tmp_path / "broken.json"
    broken.write_text("{ broken", encoding="utf-8")
    assert config.load_json_object(broken) == {}

    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    assert config.load_json_object(array) == {}


def test_load_tolerates_bom_and_caches(tmp_path):
    p = tmp_path / "bom.json"
    p.write_text(json.dumps({"token": "t"}), encoding="utf-8-sig")
    assert config.load_json_object(p) == {"token": "t"}

    # 改写且大小变化 → mtime/size 缓存失效, 读到新值
    p.write_text(json.dumps({"token": "new", "extra": 1}), encoding="utf-8")
    assert config.load_json_object(p)["token"] == "new"


def test_load_tolerates_gbk_encoded_json(tmp_path):
    """中文 Windows 编辑器常存成 GBK; 不能静默当空配置丢弃用户设置."""
    p = tmp_path / "cfg.json"
    p.write_bytes(
        json.dumps({"mode": "paper", "_comment": "中文说明"}).encode("gbk")
    )
    assert config.load_json_object(p) == {"mode": "paper", "_comment": "中文说明"}


def test_load_returns_deep_copy(tmp_path):
    p = tmp_path / "nested.json"
    p.write_text(json.dumps({"nested": {"a": 1}}), encoding="utf-8")
    first = config.load_json_object(p)
    first["nested"]["a"] = 999
    assert config.load_json_object(p)["nested"]["a"] == 1


def test_clear_cache_forces_reread(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert config.load_json_object(p)["v"] == 1
    config.clear_cache()
    p.write_text(json.dumps({"v": 2}), encoding="utf-8")
    assert config.load_json_object(p)["v"] == 2


def test_merge_skips_underscore_keys():
    merged = config.merge_config(
        {"a": 1},
        {"_comment": "x", "b": 2},
    )
    assert merged == {"a": 1, "b": 2}


def test_merge_ignore_prefix_can_be_narrowed():
    merged = config.merge_config(
        {"a": 1},
        {"_comment": "x", "_keep": 2},
        ignore_prefix="_comment",
    )
    assert merged == {"a": 1, "_keep": 2}


def test_merge_deep_keys_merges_one_level():
    merged = config.merge_config(
        {"exchanges": {"US": "SMART", "HK": "SEHK"}, "port": 1},
        {"exchanges": {"CN": "SHSE"}, "port": 2},
        deep_keys=("exchanges",),
    )
    assert merged["exchanges"] == {"US": "SMART", "HK": "SEHK", "CN": "SHSE"}
    assert merged["port"] == 2


def test_merge_does_not_alias_inputs():
    base = {"exchanges": {"US": "SMART"}}
    merged = config.merge_config(base, {"x": 1}, deep_keys=("exchanges",))
    merged["exchanges"]["US"] = "OTHER"
    assert base["exchanges"]["US"] == "SMART"
