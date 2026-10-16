"""Maintained publication confines native update maintenance to the owning home."""
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import backup, config, update_cmd, update_cmd_config, update_cmd_maint
from hermes_cli import profiles, update_receipt


@pytest.fixture(params=[True, False, None], ids=['enabled', 'disabled', 'default'])
def profile_scope(request, tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    sibling = home / 'profiles' / 'sibling'
    sibling.mkdir(parents=True)
    setting = '' if request.param is None else (
        'updates:\n  maintained_publication:\n    enabled: ' + str(request.param).lower() + '\n')
    (home / 'config.yaml').write_text('_config_version: 12\n' + setting, encoding='utf-8')
    (sibling / 'config.yaml').write_text('_config_version: 12\n', encoding='utf-8')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(update_cmd_config, '_LAST_SIBLING_SNAPSHOTS', {'stale': 'previous'})
    return SimpleNamespace(home=home, sibling=sibling, enabled=request.param is True)


def test_quick_snapshot_keeps_current_home_and_scopes_siblings(profile_scope, monkeypatch):
    state = profile_scope
    before = (state.sibling / 'config.yaml').read_bytes()
    enumerate_homes = Mock(wraps=backup._sibling_profile_homes)
    monkeypatch.setattr(backup, '_sibling_profile_homes', enumerate_homes)
    snapshot = update_cmd_maint._run_quick_snapshots()
    assert snapshot
    assert (backup._quick_snapshot_root(state.home) / snapshot / 'config.yaml').read_bytes() == (
        state.home / 'config.yaml').read_bytes()
    assert (state.sibling / 'config.yaml').read_bytes() == before
    if state.enabled:
        enumerate_homes.assert_not_called()
        assert not backup._quick_snapshot_root(state.sibling).exists()
        assert update_cmd_config._LAST_SIBLING_SNAPSHOTS == {}
    else:
        enumerate_homes.assert_called_once()
        assert update_cmd_config._LAST_SIBLING_SNAPSHOTS['sibling']
        assert backup._quick_snapshot_root(state.sibling).is_dir()


def test_profile_sync_does_not_enumerate_or_write_other_homes(profile_scope, monkeypatch):
    from plugins import memory

    calls = []
    monkeypatch.setattr(profiles, 'list_profiles', lambda: calls.append('list') or [
        SimpleNamespace(name='sibling', path=profile_scope.sibling)])
    monkeypatch.setattr(profiles, 'seed_profile_skills', lambda path, **kw: calls.append(('seed', path)) or {})
    monkeypatch.setattr(profiles, 'backfill_profile_envs', lambda **kw: calls.append('env') or [])
    monkeypatch.setattr(memory, 'import_provider_module', lambda *args: SimpleNamespace(
        sync_honcho_profiles_quiet=lambda: calls.append('honcho') or 0))
    update_cmd_maint._sync_profiles_after_update()
    assert calls == ([] if profile_scope.enabled else [
        'list', ('seed', profile_scope.sibling), 'env', 'honcho'])


def test_config_migration_preserves_current_home_and_scopes_siblings(profile_scope, monkeypatch):
    state = profile_scope
    before = (state.sibling / 'config.yaml').read_bytes()
    enumerate_homes = Mock(wraps=backup._sibling_profile_homes)
    monkeypatch.setattr(backup, '_sibling_profile_homes', enumerate_homes)
    # Bootstrap and fresh completion supply no sibling snapshot debt.
    monkeypatch.setattr(update_cmd_config, '_LAST_SIBLING_SNAPSHOTS', {})
    monkeypatch.setattr(config, 'get_missing_env_vars', lambda **kw: [])
    monkeypatch.setattr(config, 'get_missing_config_fields', list)
    update_cmd_config._check_and_apply_config_migration(assume_yes=True)
    assert config.check_config_version()[0] == config.DEFAULT_CONFIG['_config_version']
    if state.enabled:
        enumerate_homes.assert_not_called()
        assert (state.sibling / 'config.yaml').read_bytes() == before
    else:
        enumerate_homes.assert_called_once()
        assert (state.sibling / 'config.yaml').read_bytes() != before


def test_completion_transport_freezes_only_allowed_snapshot_debt(profile_scope):
    token = update_receipt._current.set(None)
    try:
        request = update_cmd._source_completion_request(
            SimpleNamespace(assume_yes=True, pre_update_version=None), None, None, None, False, False)
        assert request['managed_publication_required'] is profile_scope.enabled
        assert request['sibling_snapshots'] == ({} if profile_scope.enabled else {'stale': 'previous'})
        update_cmd_config._LAST_SIBLING_SNAPSHOTS.clear()
        assert request['sibling_snapshots'] == ({} if profile_scope.enabled else {'stale': 'previous'})
    finally:
        update_receipt._current.reset(token)


def test_current_home_bundled_sync_remains_native(profile_scope):
    update_cmd_maint._print_bundled_skills_sync_report()
    bundled = Path(__file__).resolve().parents[2] / 'skills'
    witness = next(bundled.rglob('SKILL.md'))
    assert (profile_scope.home / 'skills' / witness.relative_to(bundled)).read_bytes() == witness.read_bytes()
    assert not (profile_scope.sibling / 'skills').exists()


def test_database_restore_refusal_preserves_current_home_and_scopes_siblings(profile_scope, monkeypatch):
    state = profile_scope
    for home in (state.home, state.sibling):
        snapshot = backup._quick_snapshot_root(home) / '20261009-pre-update' / 'state.db'
        snapshot.parent.mkdir(parents=True)
        with sqlite3.connect(snapshot) as db:
            db.execute('CREATE TABLE witness (value TEXT)')
            db.execute("INSERT INTO witness VALUES ('preserved')")
        (home / 'state.db').write_bytes(b'corrupt database')
    # Upstream now refuses in-place restore when the canonical epoch is unreadable.
    # Preserve that safety behavior while proving which profile homes are checked.
    verify = Mock(wraps=update_cmd_maint._verify_and_restore_one_state_db)
    monkeypatch.setattr(update_cmd_maint, '_verify_and_restore_one_state_db', verify)
    enumerate_homes = Mock(wraps=backup._sibling_profile_homes)
    monkeypatch.setattr(backup, '_sibling_profile_homes', enumerate_homes)
    update_cmd_maint._verify_and_restore_state_dbs_post_update()
    assert (state.home / 'state.db').read_bytes() == b'corrupt database'
    assert (state.sibling / 'state.db').read_bytes() == b'corrupt database'
    expected = [state.home] if state.enabled else [state.home, state.sibling]
    assert [call.args[0] for call in verify.call_args_list] == expected
    for home in expected:
        snapshot = backup._quick_snapshot_root(home) / '20261009-pre-update' / 'state.db'
        with sqlite3.connect(snapshot) as db:
            assert db.execute('SELECT value FROM witness').fetchall() == [('preserved',)]
    if state.enabled:
        enumerate_homes.assert_not_called()
    else:
        enumerate_homes.assert_called_once()


def test_catalog_invalidation_keeps_current_home_and_scopes_siblings(profile_scope, monkeypatch):
    state = profile_scope
    for home in (state.home, state.sibling):
        cache = home / 'cache' / 'plugin-catalog.json'
        cache.parent.mkdir()
        cache.write_text('{"entries": [], "removed": []}', encoding='utf-8')
    peer_cache = state.sibling / 'cache' / 'plugin-catalog.json'
    before = peer_cache.read_bytes()
    enumerate_homes = Mock(wraps=backup._sibling_profile_homes)
    monkeypatch.setattr(backup, '_sibling_profile_homes', enumerate_homes)
    update_cmd_maint._invalidate_live_plugin_catalog_caches()
    assert not (state.home / 'cache' / 'plugin-catalog.json').exists()
    if state.enabled:
        enumerate_homes.assert_not_called()
        assert peer_cache.read_bytes() == before
    else:
        enumerate_homes.assert_called_once()
        assert not peer_cache.exists()


def test_relay_migration_keeps_current_home_and_scopes_siblings(profile_scope, monkeypatch):
    from hermes_cli import relay_plugin_migrate as relay

    state = profile_scope
    legacy = 'HERMES_NEMO_RELAY_ATOF_ENABLED=1\n'
    for home in (state.home, state.sibling):
        (home / '.env').write_text(legacy, encoding='utf-8')
    # Exercise the real leaf writer without requiring the optional Relay dependency.
    monkeypatch.setattr(relay, 'validate_relay_plugin_payload', lambda payload: [])
    enumerate_homes = Mock(wraps=profiles._iter_named_profile_dirs)
    monkeypatch.setattr(profiles, '_iter_named_profile_dirs', enumerate_homes)
    update_cmd_maint._migrate_relay_exporter_env()
    assert (state.home / relay.RELAY_PLUGINS_TOML_NAME).is_file()
    assert relay.RELAY_PLUGINS_CONFIG_ENV + '=' in (state.home / '.env').read_text(encoding='utf-8')
    if state.enabled:
        enumerate_homes.assert_not_called()
        assert (state.sibling / '.env').read_text(encoding='utf-8') == legacy
        assert not (state.sibling / relay.RELAY_PLUGINS_TOML_NAME).exists()
    else:
        enumerate_homes.assert_called_once()
        assert (state.sibling / relay.RELAY_PLUGINS_TOML_NAME).is_file()
        assert (state.sibling / '.env').read_text(encoding='utf-8') != legacy
