"""On-demand discovery and native preparation keep one verified-source boundary."""
import json
from pathlib import Path
import sys

import pytest

from hermes_cli import update_prepared as prepared
from hermes_cli import update_prepared_request as request
from hermes_cli.update_prepared_notes import PreparationRequired
from tests.hermes_cli.test_update_prepared import git, remote_graph_adapter

pytest_plugins = ["tests.hermes_cli.test_update_prepared"]
pytestmark = pytest.mark.platforms("posix")


def test_stream_exposes_progress_and_demands_explicit_ready_result(tmp_path, capsys):
    program = ("import json; print('Checking dependencies', flush=True); "
               "print('HERMES_PREPARED_RESULT='+json.dumps({'schema_version':1,'status':'ready'}),flush=True)")
    result = request._stream([sys.executable, '-c', program], tmp_path, tmp_path,
                             tmp_path / 'progress.log', timeout=5)
    assert result['status'] == 'ready'
    assert 'Checking dependencies' in capsys.readouterr().out
    assert 'HERMES_PREPARED_RESULT=' in (tmp_path / 'progress.log').read_text()
    assert (tmp_path / 'progress.log').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('terminal,exit_code', [('held', 1), ('held', 0), ('ready', 1)])
def test_failure_or_nonzero_cannot_claim_success(tmp_path, terminal, exit_code):
    program = ('import json; print("HERMES_PREPARED_RESULT="+json.dumps('
               f'{{"schema_version":1,"status":"{terminal}","reason":"Codex could not verify repair"}})); '
               f'raise SystemExit({exit_code})')
    with pytest.raises(PreparationRequired, match='Codex could not verify repair'):
        request._stream([sys.executable, '-c', program], tmp_path, tmp_path,
                        tmp_path / 'held.log', timeout=5)


def test_hung_coordinator_is_bounded_without_a_completion_record(tmp_path):
    program = 'import threading; threading.Event().wait()'
    with pytest.raises(PreparationRequired, match='explicitly requested timeout'):
        request._stream([sys.executable, '-c', program], tmp_path, tmp_path,
                        tmp_path / 'bounded.log', timeout=0.1)


def test_discovery_advertises_upstream_delta_without_mutating_source(feed, monkeypatch):
    from hermes_cli import source_check

    config_path = feed.home / 'config.yaml'
    config = json.loads(config_path.read_text())
    from hermes_cli.update_channel import install_id
    config['updates']['prepared_channels'][install_id(feed.root)]['prepare_command'] = ['/usr/bin/python3', '/trusted/preparer.py']
    config_path.write_text(json.dumps(config))
    incoming = 'c' * 40
    monkeypatch.setattr(source_check, '_branch_tip', lambda repo, branch, *_a: (
        (incoming if repo == source_check.OFFICIAL_REPOSITORY else feed.target), False, None))
    monkeypatch.setattr(source_check, '_behind_count', lambda *_a: (0, []))
    remote_graph_adapter(feed, monkeypatch)
    before = git(feed.root, 'rev-parse', 'HEAD'), git(feed.root, 'status', '--porcelain')
    status = source_check.check_for_updates(install_root=feed.root, home=feed.home, force=True)
    assert status['upstreamPending'] is True
    assert status['upstreamSha'] == incoming
    assert status['updateAvailable'] is True and status['behind'] == -1
    assert status['targetSha'] == feed.target
    assert 'during this Update' in status['message']
    assert (git(feed.root, 'rev-parse', 'HEAD'), git(feed.root, 'status', '--porcelain')) == before
    monkeypatch.setattr(request, 'upstream_tip', lambda *_a: (_ for _ in ()).throw(PreparationRequired('offline')))
    failed = source_check.check_for_updates(install_root=feed.root, home=feed.home, force=True)
    assert failed['error'] == 'preparation-required'
    assert failed['behind'] is None and failed['updateAvailable'] is False


def test_requested_upstream_must_be_in_admitted_candidate(feed):
    with pytest.raises(PreparationRequired):
        prepared.admit_target(feed.root, {'requested_upstream_sha': 'd' * 40}, prepared.policy(feed.root))
    assert git(feed.root, 'rev-parse', 'HEAD') == feed.base
    admitted = prepared.admit_target(feed.root, {'requested_upstream_sha': feed.target}, prepared.policy(feed.root))
    assert admitted == feed.target


def test_owning_policy_scopes_the_direct_coordinator_and_records_result(feed, monkeypatch):
    from hermes_cli import update_receipt

    settings = {**feed.settings, 'owner_home': str(feed.home),
                'prepare_command': [sys.executable, '-c', 'fixture']}
    monkeypatch.setattr(prepared, 'policy', lambda _root: settings)
    monkeypatch.setattr(request, 'upstream_tip', lambda _root: feed.target)
    facts, stages = [], []
    monkeypatch.setattr(update_receipt, 'record_fact', lambda *args: facts.append(args))
    monkeypatch.setattr(update_receipt, 'record_stage', lambda *args, **kwargs: stages.append(args))
    def coordinator(command, root, home, log):
        assert command[-5:] == ['--on-demand', '--upstream-sha', feed.target, '--lock-timeout', '0']
        assert root == feed.root and home == feed.home
        return {'schema_version': 1, 'status': 'ready', 'requested_upstream_sha': feed.target,
                'ready_sha': feed.target}
    monkeypatch.setattr(request, '_stream', coordinator)
    assert request.ensure_current(feed.root) == feed.target
    assert stages == [('preparation', 'running'), ('preparation', 'success')]
    assert facts[-1][0] == 'preparation_result' and facts[-1][1]['status'] == 'ready'


def test_held_preparation_stops_native_run_with_diagnostics(feed, monkeypatch, capsys):
    from hermes_cli import update_receipt, update_cmd_common

    monkeypatch.setattr(prepared, 'policy', lambda _root: {**feed.settings, 'owner_home': str(feed.home),
                        'prepare_command': [sys.executable]})
    monkeypatch.setattr(request, 'upstream_tip', lambda _root: feed.target)
    monkeypatch.setattr(request, '_stream', lambda *_: (_ for _ in ()).throw(PreparationRequired('Repair failed verification')))
    facts = []
    monkeypatch.setattr(update_receipt, 'record_fact', lambda *args: facts.append(args))
    monkeypatch.setattr(update_receipt, 'record_stage', lambda *_a, **_kw: None)
    monkeypatch.setattr(update_cmd_common, '_record_stop', lambda *_: None)
    with pytest.raises(SystemExit) as caught:
        request.ensure_current(feed.root)
    assert caught.value.code == 1
    assert facts[-1][1]['status'] == 'held'
    assert 'Repair failed verification' in capsys.readouterr().out
    assert git(feed.root, 'rev-parse', 'HEAD') == feed.base


def test_native_apply_prepares_before_backup_or_gateway_pause(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import main, update_cmd, gitlock

    monkeypatch.setattr(main, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(update_cmd._commit, 'begin_update_attempt', lambda: None)
    monkeypatch.setattr(prepared, 'prepare_args', lambda *_: None)
    monkeypatch.setattr(update_cmd, 'git_operation_in_progress', lambda *_: None)
    monkeypatch.setattr(gitlock, 'release_dead_index_lock', lambda *_: False)
    monkeypatch.setattr(update_cmd._check, 'clear_git_debris', lambda *_: None)
    monkeypatch.setattr(update_cmd, '_resolve_update_options', lambda *_: SimpleNamespace(gw_input_fn=None, assume_yes=True))
    calls = []
    monkeypatch.setattr(update_cmd, '_begin_update_receipt_and_plan', lambda *_: calls.append('receipt'))
    monkeypatch.setattr(main, '_run_pre_update_backup', lambda *_: pytest.fail('backup ran before preparation'))
    monkeypatch.setattr(update_cmd, '_pause_gateways_for_update', lambda *_: pytest.fail('gateway paused before preparation'))
    def held(root):
        assert root == tmp_path and calls == ['receipt']
        raise SystemExit(1)
    monkeypatch.setattr(request, 'ensure_current', held)
    with pytest.raises(SystemExit) as caught:
        update_cmd._cmd_update_impl(SimpleNamespace(), gateway_mode=False)
    assert caught.value.code == 1


def test_backend_check_preserves_pending_update_message(monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace
    from hermes_cli import source_check
    from hermes_cli.web_routers import actions

    monkeypatch.setattr(actions, 'is_commit_build', lambda *_: False)
    monkeypatch.setattr(actions, '_dashboard_local_update_managed_externally', lambda: False)
    monkeypatch.setattr(actions, 'detect_install_method', lambda *_: 'git')
    monkeypatch.setattr(actions, 'get_version_info', lambda: SimpleNamespace(derived_version='fixture'))
    monkeypatch.setattr(actions, '_config_profile_scope', lambda *_: nullcontext())
    monkeypatch.setattr(source_check, 'check_for_updates', lambda **_: {
        'behind': -1, 'upstreamPending': True, 'updateAvailable': True,
        'branch': 'prepared/fixture', 'message': 'Preparing and verifying during this Update.'})
    result = asyncio.run(actions.check_hermes_update(force=True))
    assert result['update_available'] is True and result['can_apply'] is True
    assert result['upstream_pending'] is True and result['behind'] == -1
    assert result['branch'] == 'prepared/fixture'
    assert result['message'] == 'Preparing and verifying during this Update.'


def test_long_preparation_refreshes_native_runtime_plan(tmp_path, monkeypatch):
    from hermes_cli import update_inventory, update_receipt

    stale, fresh, recorded = object(), object(), []
    monkeypatch.setattr(request, 'ensure_current', lambda _: 'a' * 40)
    monkeypatch.setattr(update_inventory, 'collect_runtime_inventory', lambda: fresh)
    monkeypatch.setattr(update_inventory, 'record_plan_in_receipt', recorded.append)
    monkeypatch.setattr(update_receipt, 'record_stage', lambda *_a, **_kw: None)
    plan, tip = request.prepare_and_refresh_plan(tmp_path, stale)
    assert plan is fresh and tip == 'a' * 40 and recorded == [fresh]


@pytest.mark.parametrize('stale', ['runtime-changed', 'missing-ready'])
def test_preparable_inputs_keep_update_available_without_admitting_source(feed, monkeypatch, stale):
    from hermes_cli import source_check
    from hermes_cli.update_channel import install_id

    config_path = feed.home / 'config.yaml'
    config = json.loads(config_path.read_text())
    config['updates']['prepared_channels'][install_id(feed.root)]['prepare_command'] = ['/usr/bin/python3', '/trusted/preparer.py']
    if stale == 'runtime-changed':
        config['updates']['required_plugins'] = ['newly-required']
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(source_check, '_branch_tip', lambda repo, branch, *_a: (
        (feed.target, False, None) if repo == source_check.OFFICIAL_REPOSITORY or stale != 'missing-ready'
        else (None, True, None)))
    monkeypatch.setattr(source_check, '_behind_count', lambda *_a: (0, []))
    remote_graph_adapter(feed, monkeypatch)
    status = source_check.check_for_updates(install_root=feed.root, home=feed.home, force=True)
    assert status['preparationPending'] is True and status['updateAvailable'] is True
    assert status['behind'] == -1 and 'error' not in status
    assert 'preparedValidation' not in status
    assert git(feed.root, 'rev-parse', 'HEAD') == feed.base
    if stale == 'runtime-changed':
        with pytest.raises(PreparationRequired, match='changed since preparation'):
            prepared.admit_target(feed.root, {}, prepared.policy(feed.root))


def test_native_preparation_has_no_implicit_wall_clock_limit(tmp_path):
    program = "import json;print('HERMES_PREPARED_RESULT='+json.dumps({'schema_version':1,'status':'ready'}))"
    result = request._stream([sys.executable, '-c', program], tmp_path, tmp_path, tmp_path / 'unlimited.log')
    assert result['status'] == 'ready'
