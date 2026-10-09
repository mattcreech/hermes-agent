"""Maintained publication confines native update maintenance to the owning home."""
from pathlib import Path
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
