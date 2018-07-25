# This program is free software: you can redistribute it
# and/or modify it under the terms of the GPL licence

import logging
import dill as pickle
import pytest
import time
from pytest_reportportal import LAUNCH_WAIT_TIMEOUT
from .service import PyTestServiceClass
from .listener import RPReportListener

try:
    # This try/except can go away once we support pytest >= 3.3
    import _pytest.logging
    PYTEST_HAS_LOGGING_PLUGIN = True
except ImportError:
    PYTEST_HAS_LOGGING_PLUGIN = False

log = logging.getLogger(__name__)


def is_master(config):
    """
    True if the code running the given pytest.config object is running in a xdist master
    node or not running xdist at all.
    """
    return not hasattr(config, 'slaveinput')


def get_rp_property(config, prop_name):
    try:
        value = getattr(config.option, prop_name, None)
        if not value:
            value = config.getini(prop_name)
    except AttributeError as e:
        raise AttributeError('{} rp property not set.'.format(prop_name))

    return value

@pytest.mark.optionalhook
def pytest_configure_node(node):
    if node.config._reportportal_enabled is False:
        # Stop now if the plugin is not properly configured
        return
    node.slaveinput['py_test_service'] = pickle.dumps(node.config.py_test_service)


def pytest_sessionstart(session):
    if session.config.getoption('--collect-only', default=False) is True:
        return

    if session.config._reportportal_configured is False:
        # Stop now if the plugin is not properly configured
        return

    if is_master(session.config):
        session.config.py_test_service.init_service(
            project=get_rp_property(session.config, 'rp_project'),
            endpoint=get_rp_property(session.config, 'rp_endpoint'),
            uuid=get_rp_property(session.config, 'rp_uuid'),
            log_batch_size=int(get_rp_property(session.config, 'rp_log_batch_size')),
            ignore_errors=bool(get_rp_property(session.config, 'rp_ignore_errors')),
            ignored_tags=get_rp_property(session.config, 'rp_ignore_tags'),
        )

        session.config.py_test_service.start_launch(
            get_rp_property(session.config, 'rp_launch'),
            tags=get_rp_property(session.config, 'rp_launch_tags'),
            description=get_rp_property(session.config, 'rp_launch_description'),
            mode=get_rp_property(session.config, 'rp_mode')
        )
        if session.config.pluginmanager.hasplugin('xdist'):
            wait_launch(session.config.py_test_service.RP.rp_client)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    if session.config._reportportal_configured is False:
        # Stop now if the plugin is not properly configured
        return

    # Items need to be sorted so that we can hierarchically report
    # * test-filename:
    #   * Test Suite:
    #     * Test case
    #
    # Hopefully sorting by fspath and parnt name will allow proper
    # order between test modules and any test classes.
    # We don't sort by nodeid because that changes the order of
    # parametrized tests which can rely on that order
    items.sort(key=lambda f: (f.fspath, f.parent.name))


def pytest_collection_finish(session):
    if session.config.getoption('--collect-only', default=False) is True:
        return

    if session.config._reportportal_configured is False:
        # Stop now if the plugin is not properly configured
        return

    if is_master(session.config):
        session.config.py_test_service.collect_tests(session)


def wait_launch(rp_client):
    timeout = time.time() + LAUNCH_WAIT_TIMEOUT
    while not rp_client.launch_id:
        if time.time() > timeout:
            raise Exception("Launch not found")
        time.sleep(1)


def pytest_sessionfinish(session):
    if session.config.getoption('--collect-only', default=False) is True:
        return

    if session.config._reportportal_configured is False:
        # Stop now if the plugin is not properly configured
        return

    # FixMe: currently method of RP api takes the string parameter
    # so it is hardcoded
    if is_master(session.config):
        session.config.py_test_service.finish_launch(status='RP_Launch')

    session.config.py_test_service.terminate_service()


def pytest_configure(config):
    project = get_rp_property(config, 'rp_project')
    endpoint = get_rp_property(config, 'rp_endpoint')
    uuid = get_rp_property(config, 'rp_uuid')
    config._reportportal_configured = all([project, endpoint, uuid])
    if config._reportportal_configured is False:
        return

    if is_master(config):
        config.py_test_service = PyTestServiceClass()
    else:
        config.py_test_service = pickle.loads(config.slaveinput['py_test_service'])
        config.py_test_service.RP.listener.start()

    # set Pytest_Reporter and configure it
    if PYTEST_HAS_LOGGING_PLUGIN:
        # This check can go away once we support pytest >= 3.3
        log_level = _pytest.logging.get_actual_log_level(config, 'rp_log_level')
        if log_level is None:
            log_level = logging.NOTSET
    else:
        log_level = logging.NOTSET

    config._reporter = RPReportListener(config.py_test_service,
                                        log_level=log_level,
                                        endpoint=endpoint)

    if hasattr(config, '_reporter'):
        config.pluginmanager.register(config._reporter)


def pytest_unconfigure(config):
    if config._reportportal_configured is False:
        # Stop now if the plugin is not properly configured
        return

    if hasattr(config, '_reporter'):
        reporter = config._reporter
        del config._reporter
        config.pluginmanager.unregister(reporter)
        log.debug('RP is unconfigured')


def pytest_addoption(parser):
    group = parser.getgroup('reporting')

    group.addoption(
        '--rp-uuid',
        action='store',
        dest='rp_uuid',
        help='UUID')

    group.addoption(
        '--rp-endpoint',
        action='store',
        dest='rp_endpoint',
        help='Server endpoint')

    group.addoption(
        '--rp-launch',
        action='store',
        dest='rp_launch',
        help='Launch name (overrides rp_launch config option)')

    group.addoption(
        '--rp-launch-description',
        action='store',
        dest='rp_launch_description',
        help='Launch description (overrides rp_launch_description config option)')

    group.addoption(
        '--rp-launch-tags',
        action='store',
        nargs='+',
        dest='rp_launch_tags',
        help='Launch tags (overrides rp_launch_tags config option)')

    group.addoption(
        '--rp-mode',
        action='store',
        dest='rp_mode',
        choices=['DEFAULT', 'DEBUG'],
        help='rp.mode property. (overrides rp_mode config option)'
    )

    if PYTEST_HAS_LOGGING_PLUGIN:
        group.addoption(
            '--rp-log-level',
            dest='rp_log_level',
            default=None,
            help='Logging level for automated log records reporting'
        )
        parser.addini(
            'rp_log_level',
            default=None,
            help='Logging level for automated log records reporting'
        )

    parser.addini(
        'rp_uuid',
        help='UUID')

    parser.addini(
        'rp_endpoint',
        help='Server endpoint')

    parser.addini(
        'rp_project',
        help='Project name')

    parser.addini(
        'rp_launch',
        default='Pytest Launch',
        help='Launch name')

    parser.addini(
        'rp_launch_tags',
        type='args',
        help='Launch tags, i.e Performance Regression')

    parser.addini(
        'rp_launch_description',
        default='',
        help='Launch description')

    parser.addini(
        'rp_log_batch_size',
        default='20',
        help='Size of batch log requests in async mode')

    parser.addini(
        'rp_ignore_errors',
        default=False,
        type='bool',
        help='Ignore Report Portal errors (exit otherwise)')

    parser.addini(
        'rp_ignore_tags',
        type='args',
        help='Ignore specified pytest markers, i.e parametrize')

    parser.addini(
        'rp_mode',
        default='DEFAULT',
        help='rp.mode. Value can be DEFAULT or DEBUG'
    )
